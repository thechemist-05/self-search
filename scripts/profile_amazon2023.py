#!/usr/bin/env python
"""Profile verified Amazon Reviews 2023 files against the approved selection rules. Read-only.

    python scripts/profile_amazon2023.py --category fashion --out data/experiments/profile-<ts>

Writes ``profile_<category>.json`` and ``candidates_<category>.tsv`` to ``--out``.
Nothing is written to any database, no estimates are generated, no index is built.

Refuses files that ``scripts/fetch_amazon2023.py`` has not verified (final name plus
a ``SOURCE.json`` entry whose verified sha256 equals the published one).

Memory: product metadata is parsed line by line; review lines are scanned as bytes
and only lines whose ``parent_asin`` is a candidate product are JSON-parsed and run
through the adapter. Per product only counters and up to ``DEDUP_TRACK`` 8-byte
review fingerprints are kept.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.fetch_amazon2023 import FILES, REVISION, ROOT  # noqa: E402
from src.data.adapters.amazon_reviews_2023 import AmazonReviews2023Adapter  # noqa: E402
from src.data.adapters.base import Rejection  # noqa: E402
from src.data.selection import allocate, load_selection_config, title_key  # noqa: E402

PROJECT = Path(__file__).resolve().parents[1]
CATEGORY_FILES = {
    "fashion": ("raw/meta_categories/meta_Amazon_Fashion.jsonl", "raw/review_categories/Amazon_Fashion.jsonl"),
    "electronics": ("raw/meta_categories/meta_Electronics.jsonl", "raw/review_categories/Electronics.jsonl"),
}
DEDUP_TRACK = 64
PARENT_RE = re.compile(rb'"parent_asin":\s*"((?:[^"\\]|\\.)*)"')
INR_MARKERS = re.compile(r"₹|\binr\b|\brs\.?\s*\d|rupee", re.IGNORECASE)


@dataclass
class Candidate:
    type_key: str
    title_key: str = ""
    reviews_matched: int = 0
    qualifying: int = 0
    qualifying_verified: int = 0
    short_text: int = 0
    duplicates: int = 0
    rejected: Counter = field(default_factory=Counter)
    seen: set = field(default_factory=set)


def verified_path(name: str) -> Path:
    source = json.loads((ROOT / "SOURCE.json").read_text(encoding="utf-8"))
    entry = source["files"].get(name)
    path = ROOT / name
    size, published = FILES[name]
    if not entry or entry["verified_sha256"] != published or not path.exists() or path.stat().st_size != size:
        sys.exit(f"REFUSED: {name} is not a verified download")
    return path


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def profile_meta(path: Path, rules, adapter) -> tuple[dict, dict[str, Candidate], set]:
    stats: dict = {"lines": 0, "malformed": 0, "accepted": 0, "duplicate_parent_asin": 0,
                   "rejections": Counter(), "coverage": Counter(), "currency": Counter(),
                   "price_raw_nonnumeric": 0, "inr_markers_in_price_or_title": 0,
                   "candidates_by_type": Counter(), "untyped": 0}
    candidates: dict[str, Candidate] = {}
    all_ids: set = set()
    accepted_ids: set = set()
    with open(path, "rb") as fh:
        for n, raw in enumerate(fh, 1):
            stats["lines"] += 1
            try:
                rec = json.loads(raw)
                if not isinstance(rec, dict):
                    raise ValueError("not an object")
            except ValueError:
                stats["malformed"] += 1
                continue
            rec["_line"] = n
            if rec.get("parent_asin"):
                all_ids.add(str(rec["parent_asin"]).strip())
            for item in adapter.parse_products([rec]):
                if isinstance(item, Rejection):
                    stats["rejections"][item.reason] += 1
                    continue
                if item.external_id in accepted_ids:  # first occurrence wins, as in the pipeline
                    stats["duplicate_parent_asin"] += 1
                    continue
                accepted_ids.add(item.external_id)
                stats["accepted"] += 1
                cov = stats["coverage"]
                cov["title"] += 1
                cov["description"] += item.description is not None
                cov["brand_details_Brand"] += item.brand is not None
                cov["average_rating"] += item.avg_rating is not None
                cov["rating_number"] += item.rating_count is not None
                cov["features"] += bool(item.attributes.get("features"))
                cov["category_path"] += bool(item.category_path)
                cov["source_price"] += item.price is not None
                stats["currency"][item.currency or "none"] += 1
                stats["price_raw_nonnumeric"] += "price_raw" in item.attributes
                if INR_MARKERS.search(f"{item.attributes.get('price_raw', '')} {item.title}"):
                    stats["inr_markers_in_price_or_title"] += 1
                type_key = rules.type_for(item.title)
                if type_key is None:
                    stats["untyped"] += 1
                else:
                    stats["candidates_by_type"][type_key] += 1
                    candidates[item.external_id] = Candidate(type_key, title_key(item.title))
            if n % 250_000 == 0:
                log(f"meta {n:,} lines, {len(candidates):,} candidates")
    return stats, candidates, all_ids


def profile_reviews(path: Path, candidates: dict[str, Candidate], all_ids: set, adapter, min_chars: int) -> dict:
    stats = {"lines": 0, "no_parent_asin": 0, "malformed": 0, "match_any_product": 0,
             "match_candidate": 0, "no_matching_product": 0}
    with open(path, "rb") as fh:
        for n, raw in enumerate(fh, 1):
            stats["lines"] += 1
            m = PARENT_RE.search(raw)
            if not m:
                try:
                    json.loads(raw)
                    stats["no_parent_asin"] += 1
                except ValueError:
                    stats["malformed"] += 1
                continue
            pid = m.group(1).decode("utf-8", "replace").strip()
            if pid not in all_ids:
                stats["no_matching_product"] += 1
                continue
            stats["match_any_product"] += 1
            cand = candidates.get(pid)
            if cand is None:
                continue
            stats["match_candidate"] += 1
            cand.reviews_matched += 1
            try:
                rec = json.loads(raw)
            except ValueError:
                stats["malformed"] += 1
                continue
            for item in adapter.parse_reviews([rec]):
                if isinstance(item, Rejection):
                    cand.rejected[item.reason] += 1
                    continue
                if item.product_external_id != pid:  # exact identifier match only
                    cand.rejected["parent_asin_mismatch"] += 1
                    continue
                fp = bytes.fromhex(item.external_id[5:21])
                if fp in cand.seen:
                    cand.duplicates += 1
                    continue
                if len(cand.seen) < DEDUP_TRACK:
                    cand.seen.add(fp)
                if len(item.text) < min_chars:
                    cand.short_text += 1
                    continue
                cand.qualifying += 1
                cand.qualifying_verified += item.verified
            if n % 5_000_000 == 0:
                log(f"reviews {n:,} lines")
    return stats


def select_products(candidates: dict[str, Candidate], cfg, rules):
    """Deterministic selection: qualifying products, balanced quota, most qualifying reviews first.

    Returns (qualifying, capacity per type, allocation per type, selected ids per type).
    """
    qualifying = {pid: c for pid, c in candidates.items() if c.qualifying >= cfg.min_qualifying_reviews}
    if cfg.dedupe_identical_titles:
        # The same listing under several ASINs: keep the one with most qualifying reviews (then lowest id).
        best: dict[str, str] = {}
        for pid, c in qualifying.items():
            key = c.title_key or pid  # an empty title is never treated as a duplicate
            cur = best.get(key)
            if cur is None or (-c.qualifying, pid) < (-qualifying[cur].qualifying, cur):
                best[key] = pid
        qualifying = {pid: qualifying[pid] for pid in best.values()}
    capacity = Counter(c.type_key for c in qualifying.values())
    alloc = allocate(rules.quota, dict(capacity))
    by_type: dict[str, list] = {}
    for pid, c in qualifying.items():
        by_type.setdefault(c.type_key, []).append((-c.qualifying, pid))
    selected = {k: [pid for _, pid in sorted(v)[: alloc.get(k, 0)]] for k, v in by_type.items()}
    return qualifying, capacity, alloc, selected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--category", choices=sorted(CATEGORY_FILES), required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--selection", default=str(PROJECT / "config" / "selection_amazon2023.toml"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg = load_selection_config(Path(args.selection))
    if args.category not in cfg.categories:
        sys.exit(f"REFUSED: {args.selection} ({cfg.version}) has no [{args.category}] rules")
    rules = cfg.categories[args.category]
    meta_name, review_name = CATEGORY_FILES[args.category]
    meta_path, review_path = verified_path(meta_name), verified_path(review_name)
    adapter = AmazonReviews2023Adapter(revision=REVISION)

    t0 = time.monotonic()
    meta, candidates, all_ids = profile_meta(meta_path, rules, adapter)
    log(f"meta done: {meta['accepted']:,} accepted, {len(candidates):,} candidates")
    reviews = profile_reviews(review_path, candidates, all_ids, adapter, cfg.min_review_chars)
    log("reviews done")

    qualifying, capacity, alloc, selected = select_products(candidates, cfg, rules)
    cap = cfg.max_reviews_per_product
    per_type = {k: {"candidates": meta["candidates_by_type"][k], "qualifying": capacity.get(k, 0),
                    "allocated": alloc.get(k, 0),
                    "reviews_if_selected": sum(min(candidates[p].qualifying, cap) for p in selected.get(k, []))}
                for k in [t.key for t in rules.types]}
    rejected = Counter()
    for c in candidates.values():
        rejected.update(c.rejected)
    result = {
        "category": args.category, "revision": REVISION, "selection_version": cfg.version,
        "selection_sha256": cfg.sha256, "files": {meta_name: FILES[meta_name][1], review_name: FILES[review_name][1]},
        "seconds": round(time.monotonic() - t0),
        "meta": {**{k: v for k, v in meta.items() if not isinstance(v, Counter)},
                 "rejections": dict(meta["rejections"]), "coverage": dict(meta["coverage"]),
                 "currency": dict(meta["currency"]), "candidates_by_type": dict(meta["candidates_by_type"]),
                 "distinct_parent_asin_any": len(all_ids)},
        "reviews": {**reviews,
                    "candidate_review_rejections": dict(rejected),
                    "candidate_duplicates": sum(c.duplicates for c in candidates.values()),
                    "candidate_short_text": sum(c.short_text for c in candidates.values()),
                    "candidate_qualifying": sum(c.qualifying for c in candidates.values()),
                    "candidate_qualifying_verified": sum(c.qualifying_verified for c in candidates.values()),
                    "dedup_tracked_per_product": DEDUP_TRACK},
        "selection": {
            "min_qualifying_reviews": cfg.min_qualifying_reviews, "min_review_chars": cfg.min_review_chars,
            "max_reviews_per_product": cap, "quota": rules.quota,
            "candidates": len(candidates), "qualifying_products": len(qualifying),
            "qualifying_reviews_capped_all": sum(min(c.qualifying, cap) for c in qualifying.values()),
            "selected_products": sum(len(v) for v in selected.values()),
            "selected_reviews": sum(t["reviews_if_selected"] for t in per_type.values()),
            "per_type": per_type,
        },
    }
    (out / f"profile_{args.category}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    with open(out / f"candidates_{args.category}.tsv", "w", encoding="utf-8") as fh:
        fh.write("parent_asin\ttype\treviews_matched\tqualifying\tqualifying_verified\tselected\n")
        chosen = {p for v in selected.values() for p in v}
        for pid in sorted(candidates):
            c = candidates[pid]
            fh.write(f"{pid}\t{c.type_key}\t{c.reviews_matched}\t{c.qualifying}\t{c.qualifying_verified}\t"
                     f"{int(pid in chosen)}\n")
    log(f"wrote {out}")


if __name__ == "__main__":
    main()
