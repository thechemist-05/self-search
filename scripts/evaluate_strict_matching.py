#!/usr/bin/env python
"""Returned-result precision of strict matching versus a relaxed baseline, per catalogue.

Usage:
    python scripts/evaluate_strict_matching.py --catalogue fashion-20k=<db>,<index_dir> \
        --catalogue fashion-50k=<db>,<index_dir> --out eval/results/strict_matching_eval.json

Each query in ``eval/strict_queries.jsonl`` goes through the real controller (rules-first
parsing, constraint resolution, hybrid retrieval with the catalogue's FAISS indexes). The
model is unavailable here, so only *retrieval* is measured, not answer text. Every product
actually returned is judged against the query's hand-written criteria (title regexes,
colour, INR budget, brand), which are independent of the rules in
``src/retrieval/constraints.py``.

Modes:
* ``strict``: the current pipeline.
* ``relaxed``: the same code with the strict rules switched off: a product type matches
  any synonym anywhere in the title or description, and colour, material and brand are
  not constraints (they only influence keyword ranking). This approximates the earlier,
  purely lexical behaviour.

Metrics per query: results returned, relevant returned, precision (relevant / returned),
zero-result flag. Aggregates: mean precision over queries with results, micro precision,
relevant-result rate (queries with >= 1 relevant result), zero-result count.
Databases are copied to a temporary file first, so they are never modified.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import os  # noqa: E402

from scripts import benchmark_llm as bench  # noqa: E402
from src.api.app import open_api_connection  # noqa: E402
from src.config.settings import get_settings  # noqa: E402
from src.llm.provider import MockLLMProvider  # noqa: E402


def load_queries(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


@contextlib.contextmanager
def relaxed_rules() -> Iterator[None]:
    """Switch strict typing and colour/material/brand constraints off for the duration."""
    from src.agent import constraints as agent_constraints
    from src.agent import query_parser
    from src.retrieval import constraints

    saved_types = dict(constraints.STRICT_TYPES)
    saved = (query_parser.detect_colours, query_parser.detect_materials, query_parser.detect_occasions,
             agent_constraints.ConstraintResolver.detect_brand)
    constraints.STRICT_TYPES.clear()
    query_parser.detect_colours = lambda _t: []          # type: ignore[assignment]
    query_parser.detect_materials = lambda _t: []        # type: ignore[assignment]
    query_parser.detect_occasions = lambda _t: []        # type: ignore[assignment]
    agent_constraints.ConstraintResolver.detect_brand = lambda self, _t: None  # type: ignore[assignment]
    try:
        yield
    finally:
        constraints.STRICT_TYPES.update(saved_types)
        (query_parser.detect_colours, query_parser.detect_materials, query_parser.detect_occasions,
         agent_constraints.ConstraintResolver.detect_brand) = saved


def _usd_inr_rate() -> float:
    """The documented USD rate, read straight from the config file (independent of src/pricing)."""
    import tomllib

    return float(tomllib.loads((PROJECT_ROOT / "config" / "exchange_rates.toml").read_text(encoding="utf-8"))["USD"]["rate"])


def judge(q: dict, product: dict[str, Any], estimate: Optional[float]) -> bool:
    """``product`` carries the *database* price and currency, not the API's conversion."""
    title = product["title"] or ""
    flags = re.IGNORECASE
    if not all(re.search(p, title, flags) for p in q.get("all", [])):
        return False
    if any(re.search(p, title, flags) for p in q.get("none", [])):
        return False
    details = (product.get("attributes") or {}).get("details") or {}
    if q.get("colour") and not (re.search(q["colour"], title, flags) or re.search(q["colour"], str(details.get("Color") or ""), flags)):
        return False
    if q.get("material") and not (re.search(q["material"], title, flags) or re.search(q["material"], str(details.get("Material") or ""), flags)):
        return False
    if q.get("brand"):
        store = str((product.get("attributes") or {}).get("store") or "")
        if not (re.search(q["brand"], store, flags) or re.search(q["brand"], title, flags)):
            return False
    if q.get("occasion"):
        text = f"{title} {product.get('description') or ''} {details.get('Occasion') or ''}"
        if not re.search(q["occasion"], text, flags):
            return False
    if q.get("max_inr") is not None:
        cur = (product.get("currency") or "").upper()
        if product.get("price") is None:
            inr = estimate
        elif cur == "INR":
            inr = product["price"]
        elif cur == "USD":
            inr = int(product["price"] * _usd_inr_rate() + 0.5)
        else:
            inr = None
        if inr is None or inr > q["max_inr"]:
            return False
    return True


def run_catalogue(name: str, db: Path, index_dir: Path, queries: list[dict], mode: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="ss-strict-eval-") as tmp:
        copy = Path(tmp) / "eval.db"
        bench.copy_database(db, copy)
        os.environ["SHELFSENSE_VECTOR_STORE_DIR"] = str(index_dir)
        os.environ["SHELFSENSE_ESTIMATED_PRICES_ENABLED"] = "true"
        get_settings.cache_clear()
        conn = open_api_connection(copy)
        raw = sqlite3.connect(copy)
        try:
            ctx = relaxed_rules() if mode == "relaxed" else contextlib.nullcontext()
            with ctx:
                controller = bench.build_controller(conn, MockLLMProvider(available=False), index_dir)
                rows = []
                for q in queries:
                    t0 = time.perf_counter()
                    r = controller.handle_message(q["query"])
                    seconds = time.perf_counter() - t0
                    judged = []
                    for card in r.products:
                        src_price, src_cur, desc = raw.execute(
                            "SELECT price, currency, description FROM products WHERE id = ?", (card.id,)).fetchone()
                        est = raw.execute("SELECT amount FROM price_estimates WHERE product_id = ?",
                                          (card.id,)).fetchone() if src_price is None else None
                        prod = {"title": card.title, "price": src_price, "currency": src_cur, "description": desc,
                                "attributes": {**(card.attributes or {})}}
                        ok = judge(q, prod, est[0] if est else None)
                        judged.append({"id": card.id, "title": card.title[:90], "relevant": ok})
                    n, rel = len(judged), sum(j["relevant"] for j in judged)
                    rows.append({"id": q["id"], "query": q["query"], "returned": n, "relevant": rel,
                                 "precision": round(rel / n, 3) if n else None, "seconds": round(seconds, 2),
                                 "matching_products": (r.constraints.matching_products if r.constraints else None),
                                 "results": judged})
                    print(f"{name:12} {mode:7} {q['id']} {q['query'][:34]:34} returned {n:2} relevant {rel:2}", flush=True)
        finally:
            conn.close()
            raw.close()
    with_results = [r for r in rows if r["returned"]]
    returned = sum(r["returned"] for r in rows)
    relevant = sum(r["relevant"] for r in rows)
    return {
        "catalogue": name, "mode": mode, "queries": len(rows),
        "mean_precision": round(statistics.mean(r["precision"] for r in with_results), 3) if with_results else None,
        "micro_precision": round(relevant / returned, 3) if returned else None,
        "relevant_result_rate": round(sum(1 for r in rows if r["relevant"]) / len(rows), 3),
        "zero_result_queries": sum(1 for r in rows if not r["returned"]),
        "results_returned": returned, "relevant_returned": relevant,
        "median_seconds": round(statistics.median(r["seconds"] for r in rows), 2),
        "per_query": rows,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalogue", action="append", required=True, help="name=<db>,<index_dir>")
    parser.add_argument("--queries", type=Path, default=PROJECT_ROOT / "eval" / "strict_queries.jsonl")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    queries = load_queries(args.queries)
    runs = []
    for spec in args.catalogue:
        name, paths = spec.split("=", 1)
        db, index_dir = (Path(p) for p in paths.split(","))
        for mode in ("relaxed", "strict"):
            runs.append(run_catalogue(name, db, index_dir, queries, mode))
    summary = [{k: v for k, v in r.items() if k != "per_query"} for r in runs]
    print(json.dumps(summary, indent=1))
    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "runs": runs}, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
