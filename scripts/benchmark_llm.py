#!/usr/bin/env python
"""Benchmark local Ollama models on the full Self Search answer pipeline.

Usage:
    python scripts/benchmark_llm.py --models llama3.2:3b llama3.2:1b \
        --out eval/results/llm_benchmark.json

Every query in ``eval/queries.jsonl`` goes through ``AgentController`` exactly as
``/chat`` does: rules-first query parsing, hybrid retrieval, evidence validation
(including numeric checks) and the safe fallback. The configured Ollama timeout
applies unchanged. The run uses a temporary copy of the database (the API writes
query logs), so the source database is never modified; the index directory is
only read and must match it (manifest check, as at API startup).

Per model it records latency, the successful LLM response rate, output validity
(first-attempt valid JSON, retries), evidence compliance of model answers and the
fallback rate.

Searches are product-first, so each query is timed in up to three parts, reported
separately: the search itself (retrieval, no model call), the optional AI summary
of its results (``AgentController.summarise_results``, as POST /chat/summary runs
it; skip with ``--no-summaries``), and any ``followups`` (comparison, review
question). For every model call it also keeps Ollama's own prompt and output token
counts and timings, which show whether the fixed prompt prefix was reused.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.agent.controller import AgentController  # noqa: E402
from src.api.app import open_api_connection  # noqa: E402
from src.config.settings import get_settings  # noqa: E402
from src.evidence.validator import EvidenceValidator  # noqa: E402
from src.llm.ollama import OllamaProvider  # noqa: E402
from src.llm.provider import LLMProvider, LLMResponse  # noqa: E402
from src.llm.service import LLMService  # noqa: E402
from src.retrieval.hybrid import HybridRetriever  # noqa: E402
from src.vector_store.manifest import require_valid_manifest  # noqa: E402


# Ollama reports these per request: prompt tokens actually evaluated (a reused prefix is
# not counted again), output tokens, and the time spent on each (nanoseconds).
OLLAMA_COUNTERS = ("prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration", "load_duration")


@dataclass
class TimedProvider(LLMProvider):
    """Wraps a provider and records the duration and outcome of every model call."""

    inner: LLMProvider
    calls: list[dict[str, Any]] = field(default_factory=list)
    last_messages: list[dict[str, str]] = field(default_factory=list)

    def generate(self, messages: list[dict[str, str]], temperature: float = 0.2, **kwargs: Any) -> LLMResponse:
        self.last_messages = messages
        start = time.perf_counter()
        try:
            response = self.inner.generate(messages, temperature=temperature, **kwargs)
        except Exception as exc:
            self.calls.append({"seconds": time.perf_counter() - start, "ok": False,
                               "error": type(exc).__name__})
            raise
        call: dict[str, Any] = {"seconds": time.perf_counter() - start, "ok": True}
        raw = response.raw_response if isinstance(response.raw_response, dict) else {}
        for key in OLLAMA_COUNTERS:  # Ollama's own accounting; absent for other providers
            if key in raw:
                call[key] = raw[key]
        self.calls.append(call)
        return response

    def is_available(self) -> bool:
        return self.inner.is_available()


def load_queries(path: Path) -> list[dict]:
    """Read the JSONL evaluation queries (blank lines and # comments skipped)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip() and not line.lstrip().startswith("#")]


def copy_database(source: Path, target: Path) -> None:
    """Consistent copy with SQLite's backup API; the source is opened read-only."""
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def build_controller(conn: sqlite3.Connection, provider: LLMProvider, index_dir: Path) -> AgentController:
    """Same wiring as ``create_default_agent_controller``, with the given provider."""
    require_valid_manifest(conn, index_dir)
    return AgentController(conn=conn, llm_provider=provider, retriever=HybridRetriever(conn=conn),
                           llm_service=LLMService(provider=provider), validator=EvidenceValidator(conn=conn))


def run_query(controller: AgentController, provider: TimedProvider, query: dict,
              summaries: bool = True) -> list[dict[str, Any]]:
    """Run one query in a fresh session: the search, its optional summary (when it found
    products), then its ``followups`` in the same session."""
    first = run_turn(controller, provider, query, None, kind="search")
    records = [first]
    if summaries and first["products_shown"] and first["intent"] in ("PRODUCT_SEARCH", "REFINE_SEARCH"):
        records.append(run_turn(controller, provider, {"query_id": f"{query.get('query_id')}+summary",
                                                       "query": query["query"]},
                                first["session_id"], kind="summary"))
    for n, text in enumerate(query.get("followups", []), start=1):
        records.append(run_turn(controller, provider, {"query_id": f"{query.get('query_id')}>{n}", "query": text},
                                first["session_id"], kind="followup"))
    return records


def run_turn(controller: AgentController, provider: TimedProvider, query: dict,
             session_id: Optional[str], kind: str = "search") -> dict[str, Any]:
    """Run one message (or, for ``kind="summary"``, the session's optional summary) and
    record what the pipeline actually did."""
    provider.calls.clear()
    provider.last_messages = []
    start = time.perf_counter()
    if kind == "summary":
        response = controller.summarise_results(session_id)
    else:
        response = controller.handle_message(query["query"], session_id=session_id)
    seconds = time.perf_counter() - start
    gen = response.generation
    report = response.support_report or {}
    relevant = set(query.get("relevant_ids", []))
    prompt_chars = sum(len(m["content"]) for m in provider.last_messages) if provider.last_messages else 0
    return {
        "query_id": query.get("query_id"), "kind": kind, "query": query["query"], "intent": response.intent.value,
        "session_id": response.session_id, "seconds": round(seconds, 2),
        "products_shown": len(response.result_product_ids), "evidence_items": len(response.evidence),
        "answer_prompt_chars": prompt_chars,
        "model_calls": [{**c, "seconds": round(c["seconds"], 2)} for c in provider.calls],
        "mode": gen.mode if gen else None, "reason": gen.reason if gen else None,
        "attempts": gen.attempts if gen else None,
        "relevant_retrieved": bool(relevant & set(response.result_product_ids)) if relevant else None,
        "evidence": {k: report.get(k) for k in (
            "total_claims", "supported_claims", "unsupported_claims", "fabricated_citations",
            "cross_product_mismatches", "numeric_mismatches", "unverifiable_claims",
            "support_rate", "is_valid")},
    }


def _rate(n: int, d: int) -> Optional[float]:
    return round(n / d, 3) if d else None


def _latency(secs: list[float]) -> dict[str, Optional[float]]:
    return {
        "median": round(statistics.median(secs), 1) if secs else None,
        "mean": round(statistics.mean(secs), 1) if secs else None,
        "max": round(max(secs), 1) if secs else None,
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-query records into the benchmark metrics.

    ``latency_seconds`` covers model answers (summaries and follow-ups). Searches,
    summaries and follow-ups are also timed separately; search times are given to
    two decimals because, without a model call, they are a few seconds at most.
    """
    answered = [r for r in records if r["mode"] in ("llm", "fallback")]
    llm = [r for r in answered if r["mode"] == "llm"]
    calls = [c for r in records for c in r["model_calls"]]
    secs = [r["seconds"] for r in answered]
    ev = [r["evidence"] for r in llm]
    searches = [r for r in records if r.get("kind") == "search"]
    search_secs = [r["seconds"] for r in searches]

    def total(key: str) -> int:
        return sum(e.get(key) or 0 for e in ev)

    def kind_secs(kind: str) -> list[float]:
        return [r["seconds"] for r in records if r.get("kind") == kind and r["mode"] in ("llm", "fallback")]

    prompt_tokens = [c["prompt_eval_count"] for c in calls if "prompt_eval_count" in c]
    return {
        "queries": len(searches),
        "turns": len(records),
        "search_latency_seconds": {
            "median": round(statistics.median(search_secs), 2) if search_secs else None,
            "mean": round(statistics.mean(search_secs), 2) if search_secs else None,
            "max": round(max(search_secs), 2) if search_secs else None,
        },
        "searches_calling_the_model": sum(1 for r in searches if r["model_calls"]),
        "summary_latency_seconds": _latency(kind_secs("summary")),
        "followup_latency_seconds": _latency(kind_secs("followup")),
        "prompt_tokens_evaluated": {
            "median": statistics.median(prompt_tokens) if prompt_tokens else None,
            "min": min(prompt_tokens) if prompt_tokens else None,
            "max": max(prompt_tokens) if prompt_tokens else None,
        },
        "answered": len(answered),
        "llm_success_rate": _rate(len(llm), len(answered)),
        "fallback_rate": _rate(len(answered) - len(llm), len(answered)),
        "fallback_reasons": dict(Counter(r["reason"] for r in answered if r["mode"] == "fallback")),
        "output_validity": {
            "first_attempt_valid_rate": _rate(sum(1 for r in llm if r["attempts"] == 1), len(answered)),
            "answers_needing_retry": sum(1 for r in llm if (r["attempts"] or 1) > 1),
            "invalid_after_retries": sum(1 for r in answered if r["reason"] == "invalid_output_after_retries"),
        },
        "evidence_compliance_llm_answers": {
            "answers": len(llm),
            "all_claims_supported_rate": _rate(sum(1 for e in ev if e.get("is_valid")), len(llm)),
            "mean_support_rate": round(statistics.mean(e.get("support_rate") or 0 for e in ev), 3) if ev else None,
            "claims": total("total_claims"), "unsupported_claims": total("unsupported_claims"),
            "fabricated_citations": total("fabricated_citations"),
            "cross_product_mismatches": total("cross_product_mismatches"),
            "numeric_mismatches": total("numeric_mismatches"),
        },
        "latency_seconds": _latency(secs),
        "model_calls": len(calls),
        "model_call_errors": dict(Counter(c["error"] for c in calls if not c["ok"])),
        "relevant_retrieved_rate": _rate(sum(1 for r in records if r["relevant_retrieved"]),
                                         sum(1 for r in records if r["relevant_retrieved"] is not None)),
    }


def benchmark_model(model: str, queries: list[dict], db_path: Path, index_dir: Path,
                    provider_factory: Callable[[str], LLMProvider],
                    log: Callable[[str], None] = print, summaries: bool = True) -> dict[str, Any]:
    """Run every query with one model on its own temporary database copy."""
    with tempfile.TemporaryDirectory(prefix="ss-bench-") as tmp:
        copy = Path(tmp) / "bench.db"
        copy_database(db_path, copy)
        conn = open_api_connection(copy)
        try:
            provider = TimedProvider(provider_factory(model))
            if not provider.is_available():
                raise SystemExit(f"model provider for {model} is not available")
            controller = build_controller(conn, provider, index_dir)
            warm = time.perf_counter()
            try:  # load the model before timing; a failure here shows up again per query
                provider.generate([{"role": "user", "content": "Reply with OK."}])
            except Exception as exc:
                log(f"{model} warm-up failed: {type(exc).__name__}")
            warmup_s = round(time.perf_counter() - warm, 1)
            records = []
            for q in queries:
                for rec in run_query(controller, provider, q, summaries=summaries):
                    tokens = [c.get("prompt_eval_count") for c in rec["model_calls"] if "prompt_eval_count" in c]
                    log(f"{model} {rec['query_id']}: {rec['kind']} {rec['intent']} {rec['mode']} ({rec['reason']}) "
                        f"{rec['seconds']}s, {rec['products_shown']} products, prompt {rec['answer_prompt_chars']} chars"
                        + (f", prompt tokens evaluated {tokens}" if tokens else ""))
                    records.append(rec)
        finally:
            conn.close()
    return {"model": model, "warmup_seconds": warmup_s, "summary": summarize(records), "queries": records}


def main(argv: Optional[list[str]] = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", nargs="+", default=[settings.ollama_model])
    parser.add_argument("--queries", type=Path, default=PROJECT_ROOT / "eval" / "queries.jsonl")
    parser.add_argument("--db", type=Path, default=Path(settings.db_path))
    parser.add_argument("--index-dir", type=Path, default=Path(settings.vector_store_dir))
    parser.add_argument("--limit", type=int, default=None, help="only the first N queries")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--no-summaries", action="store_true",
                        help="time searches and follow-ups only (no optional summary per search)")
    args = parser.parse_args(argv)

    if not args.db.is_file():
        parser.error(f"database not found: {args.db}")
    os.environ["SHELFSENSE_VECTOR_STORE_DIR"] = str(args.index_dir)  # read by the retriever
    get_settings.cache_clear()
    queries = load_queries(args.queries)[: args.limit]
    result = {
        "database": str(args.db), "index_dir": str(args.index_dir), "queries_file": str(args.queries),
        "query_parser_mode": get_settings().query_parser_mode, "ollama_timeout_s": get_settings().ollama_timeout,
        "runs": [benchmark_model(m, queries, args.db, args.index_dir, lambda m: OllamaProvider(model=m),
                                 summaries=not args.no_summaries)
                 for m in args.models],
    }
    text = json.dumps(result, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    print(json.dumps([{"model": r["model"], **r["summary"]} for r in result["runs"]], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
