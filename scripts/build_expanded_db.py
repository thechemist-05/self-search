#!/usr/bin/env python
"""Build the expanded catalogue: the validated v3 catalogue plus up to 50,000 products, in the
allocation of ``config/selection_amazon2023_v4_additions.toml``, from four categories of the same
pinned Amazon Reviews 2023 revision (Clothing, Shoes & Jewelry; All Beauty; Handmade Products;
Sports and Outdoors).

    python scripts/build_expanded_db.py --run-id fashion-v4-<ts> \\
        --base data/experiments/fashion-v3-20261022T085353Z \\
        --selection config/selection_amazon2023_v4_additions.toml

Writes only to ``data/experiments/<run-id>/`` (refuses an existing directory). The base
catalogue is copied with SQLite's backup API (opened read-only) and its database hash is
checked before and after, so it is never changed.

Steps:
1. Copy the base database (50,000 products, schema v4 + experimental v3 marker).
2. Per source section, profile the verified metadata with that section's rules: products
   already in the base or chosen earlier (same parent_asin) are skipped; each type keeps its
   ``PER_TYPE_CANDIDATES`` most-rated candidates (memory bound; scarce types keep all).
3. Count qualifying reviews (same code as v3; the v4 minimum is set in the config).
4. Deduplicate: identical normalised titles and same-seller near-duplicates, against the base,
   earlier sections and within the section (most qualifying reviews kept); then allocate each
   group's quota across its sub-style types and pick by most qualifying reviews.
5. Ingest the chosen products with the same adapter and pipeline (<= 8 reviews each).
6. Synthetic INR estimates (unpriced products only), provenance, normalised INR prices
   (documented exchange rate), catalogue types and image candidates for every product.
7. Critical validation and ``dataset_manifest.json``. Exit 1 on any critical failure.

Nothing is invented: products, reviews, ratings, brands, prices and image URLs come from the
source records; missing values stay missing and are counted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_experimental_db import collect, coverage, file_sha256  # noqa: E402
from scripts.extract_product_images import pick_url  # noqa: E402
from scripts.fetch_amazon2023 import FILES, REVISION, ROOT  # noqa: E402
from scripts.profile_amazon2023 import PARENT_RE, Candidate, log, profile_reviews, verified_path  # noqa: E402
from src.data.adapters.amazon_reviews_2023 import AmazonReviews2023Adapter  # noqa: E402
from src.data.adapters.base import Rejection  # noqa: E402
from src.data.adapters.pipeline import _review_rank, ingest  # noqa: E402
from src.data.validation import validate_review  # noqa: E402
from src.data.selection import (  # noqa: E402
    allocate_groups,
    allocate_with_priority,
    load_selection_config,
    near_title_key,
    title_key,
)
from src.db.schema import SCHEMA_VERSION  # noqa: E402
from src.pricing.budget import classify_price  # noqa: E402
from src.pricing.estimates import generate_estimates, load_range_config  # noqa: E402
from src.pricing.fx import DEFAULT_FX_CONFIG, fx_config_sha256, load_fx_rates  # noqa: E402

PROJECT = Path(__file__).resolve().parents[1]
EXPERIMENTS = PROJECT / "data" / "experiments"
BASE_META = "raw/meta_categories/meta_Amazon_Fashion.jsonl"
BASE_REVIEWS = "raw/review_categories/Amazon_Fashion.jsonl"
ADD_META = "raw/meta_categories/meta_Clothing_Shoes_and_Jewelry.jsonl"
ADD_REVIEWS = "raw/review_categories/Clothing_Shoes_and_Jewelry.jsonl"
# selection section -> (metadata file, review file), processed in this order
SOURCES = {
    "clothing": (ADD_META, ADD_REVIEWS),
    "beauty": ("raw/meta_categories/meta_All_Beauty.jsonl", "raw/review_categories/All_Beauty.jsonl"),
    "handmade": ("raw/meta_categories/meta_Handmade_Products.jsonl", "raw/review_categories/Handmade_Products.jsonl"),
    "sports": ("raw/meta_categories/meta_Sports_and_Outdoors.jsonl", "raw/review_categories/Sports_and_Outdoors.jsonl"),
}
PER_TYPE_CANDIDATES = 8_000    # most-rated candidates kept per type for the review pass (memory bound)
PROFILE_CACHE = EXPERIMENTS / "_profile_cache"
MAX_ALTERNATE_IMAGES = 3

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS product_provenance (
    product_id INTEGER PRIMARY KEY REFERENCES products(id),
    source_file TEXT NOT NULL,          -- raw metadata file the record came from
    source_sha256 TEXT NOT NULL,
    selection_version TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catalogue_types (
    product_id INTEGER PRIMARY KEY REFERENCES products(id),
    type_key TEXT NOT NULL,             -- normalised catalogue category (selection rules)
    selection_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_catalogue_types_type ON catalogue_types(type_key);
CREATE TABLE IF NOT EXISTS price_normalization (
    product_id INTEGER PRIMARY KEY REFERENCES products(id),
    original_price REAL,                -- the source price, unchanged (traceability only)
    original_currency TEXT,
    inr_amount REAL,                    -- what is shown and filtered; NULL = price unavailable
    status TEXT NOT NULL CHECK (status IN ('verified', 'converted', 'unavailable')),
    reason TEXT,
    fx_rate REAL,
    fx_rate_date TEXT,
    fx_source TEXT,
    fx_version TEXT
);
"""


def _rel(path: Path) -> str:
    """Project-relative path when inside the project, else absolute."""
    try:
        return str(path.resolve().relative_to(PROJECT))
    except ValueError:
        return str(path)


def log_mem(msg: str) -> None:
    log(msg)


def wait_verified(name: str, timeout_s: int = 3 * 3600) -> Path:
    """The verified file ``name``; waits (up to ``timeout_s``) while a resumable download of it
    is still in progress, then refuses exactly like ``verified_path``. Nothing unverified is read."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        entry = json.loads((ROOT / "SOURCE.json").read_text(encoding="utf-8"))["files"].get(name)
        if entry and entry.get("verified_sha256") == FILES[name][1]:
            break
        if (ROOT / f"{name}.bad").exists():
            break
        log(f"waiting for the verified download of {name}")
        time.sleep(60)
    return verified_path(name)


def sqlite_backup(src: Path, dst: Path) -> None:
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def base_keys(conn: sqlite3.Connection) -> tuple[set[str], set[str], set[str]]:
    """parent_asins, normalised titles and near-duplicate keys of the base catalogue."""
    ids, titles, near = set(), set(), set()
    for ext, title, attrs in conn.execute("SELECT external_id, title, attributes_json FROM products"):
        ids.add(ext)
        titles.add(title_key(title))
        store = (json.loads(attrs or "{}") or {}).get("store")
        k = near_title_key(title, store)
        if k:
            near.add(k)
    return ids, titles, near


def profile_additions(meta_path: Path, rules, adapter, skip_ids: set[str], min_ratings: int = 3):
    """Typed candidates from an additions metadata file (not already chosen). Memory-light records.

    A product with fewer than ``min_ratings`` ratings cannot have that many reviews and is skipped.
    Types listed as ``priority`` keep every candidate; other types keep their most-rated
    ``PER_TYPE_CANDIDATES`` (types in a grouped section are capped the same way).
    """
    priority = {t.key for t in rules.types if t.priority}
    stats = Counter()
    pri: dict[str, tuple] = {}
    other: dict[str, list] = {}
    with open(meta_path, "rb") as fh:
        for n, raw in enumerate(fh, 1):
            try:
                rec = json.loads(raw)
            except ValueError:
                stats["malformed"] += 1
                continue
            rec["_line"] = n
            for item in adapter.parse_products([rec]):
                if isinstance(item, Rejection):
                    stats[f"rejected_{item.reason}"] += 1
                    continue
                stats["accepted"] += 1
                if item.external_id in skip_ids:
                    stats["duplicate_asin_in_base"] += 1
                    continue
                type_key = rules.type_for(item.title)
                if type_key is None:
                    stats["untyped_or_excluded"] += 1
                    continue
                if (item.rating_count or 0) < min_ratings:
                    stats[f"fewer_than_{min_ratings}_ratings"] += 1  # cannot have enough qualifying reviews
                    continue
                entry = (type_key, title_key(item.title), near_title_key(item.title, item.attributes.get("store")),
                         item.rating_count or 0)
                if type_key in priority:
                    pri[item.external_id] = entry
                else:
                    other.setdefault(type_key, []).append((-(item.rating_count or 0), item.external_id, entry))
            if n % 500_000 == 0:
                log(f"meta {n:,} lines; priority {len(pri):,}, other {sum(map(len, other.values())):,}")
    stats["candidates_priority"] = len(pri)
    stats["candidates_other_before_cap"] = sum(map(len, other.values()))
    kept = dict(pri)
    for type_key, rows in other.items():
        rows.sort()
        for _, ext, entry in rows[:PER_TYPE_CANDIDATES]:
            kept[ext] = entry
    stats["candidates_for_review_pass"] = len(kept)
    return kept, stats


def dedupe(qualifying: dict[str, tuple], q_reviews: dict[str, int], base_titles: set[str], base_near: set[str]):
    """Remove identical-title and same-seller near-duplicates, against the base and each other."""
    stats = Counter()
    keep = {}
    for ext, entry in qualifying.items():
        if entry[1] and entry[1] in base_titles:
            stats["identical_title_in_base"] += 1
        elif entry[2] and entry[2] in base_near:
            stats["near_duplicate_in_base"] += 1
        else:
            keep[ext] = entry
    for idx, label in ((1, "identical_title_within_additions"), (2, "near_duplicate_within_additions")):
        best: dict[str, str] = {}
        for ext, entry in keep.items():
            key = entry[idx]
            if not key:
                best[f"\0{ext}"] = ext  # an empty key never matches anything
                continue
            cur = best.get(key)
            if cur is None or (-q_reviews[ext], ext) < (-q_reviews[cur], cur):
                best[key] = ext
        survivors = set(best.values())
        stats[label] += len(keep) - len(survivors)
        keep = {ext: keep[ext] for ext in survivors}
    return keep, stats


def collect_reviews_bounded(path: Path, wanted: set[str], adapter, keep: int, min_chars: int) -> list[dict]:
    """Raw review records of the wanted products, at most ``keep`` per product: exactly the
    reviews ``ingest`` would keep from the full file (same parsing, duplicate removal,
    validation, minimum length and ``_review_rank`` order), without holding every review of
    a popular product in memory."""
    best: dict[str, list[tuple]] = {}
    seen: set[str] = set()
    with open(path, "rb") as fh:
        for n, raw in enumerate(fh, 1):
            m = PARENT_RE.search(raw)
            if not m or m.group(1).decode("utf-8", "replace").strip() not in wanted:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            rec["_line"] = n
            for item in adapter.parse_reviews([rec]):
                if isinstance(item, Rejection) or item.external_id in seen:
                    continue
                seen.add(item.external_id)
                if not validate_review({"text": item.text, "product_id": 1, "rating": item.rating}).is_valid:
                    continue
                if len(item.text) < min_chars:
                    continue
                group = best.setdefault(item.product_external_id, [])
                group.append((_review_rank(item), rec))
                if len(group) > 4 * keep:
                    group.sort(key=lambda x: x[0])
                    del group[keep:]
    out = []
    for group in best.values():
        group.sort(key=lambda x: x[0])
        out.extend(rec for _, rec in group[:keep])
    return out


def _profile_range(args):
    """One worker of ``profile_additions_parallel``: the same loop over one byte range."""
    meta_path, start, end, rules, revision, skip_ids, min_ratings = args
    adapter = AmazonReviews2023Adapter(revision=revision)
    priority = {t.key for t in rules.types if t.priority}
    stats = Counter()
    pri, other = {}, {}
    with open(meta_path, "rb") as fh:
        if start:
            # A line belongs to the worker whose range holds its first byte: step back one byte
            # and drop the rest of that line (only the newline when ``start`` is already a line start).
            fh.seek(start - 1)
            fh.readline()
        while fh.tell() <= end:
            raw = fh.readline()
            if not raw:
                break
            try:
                rec = json.loads(raw)
            except ValueError:
                stats["malformed"] += 1
                continue
            rec["_line"] = 0
            for item in adapter.parse_products([rec]):
                if isinstance(item, Rejection):
                    stats[f"rejected_{item.reason}"] += 1
                    continue
                stats["accepted"] += 1
                if item.external_id in skip_ids:
                    stats["duplicate_asin_in_base"] += 1
                    continue
                type_key = rules.type_for(item.title)
                if type_key is None:
                    stats["untyped_or_excluded"] += 1
                    continue
                if (item.rating_count or 0) < min_ratings:
                    stats[f"fewer_than_{min_ratings}_ratings"] += 1
                    continue
                entry = (type_key, title_key(item.title), near_title_key(item.title, item.attributes.get("store")),
                         item.rating_count or 0)
                if type_key in priority:
                    pri[item.external_id] = entry
                else:
                    other.setdefault(type_key, []).append((-(item.rating_count or 0), item.external_id, entry))
        # keep each type's best candidates only, so the merged lists stay small
        other = {k: sorted(v)[:PER_TYPE_CANDIDATES] for k, v in other.items()}
    return pri, other, stats


def profile_additions_parallel(meta_path: Path, rules, revision: str, skip_ids: set[str], min_ratings: int,
                               workers: int = 6):
    """``profile_additions`` split over byte ranges of the file in worker processes. Same records,
    same rules, same result (first occurrence of a parent_asin wins, as in the serial pass)."""
    from concurrent.futures import ProcessPoolExecutor

    size = meta_path.stat().st_size
    bounds = [size * i // workers for i in range(workers + 1)]
    jobs = [(meta_path, bounds[i], bounds[i + 1] - 1 if i < workers - 1 else size, rules, revision,
             skip_ids, min_ratings) for i in range(workers)]
    stats, pri, merged = Counter(), {}, {}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for part_pri, part_other, part_stats in pool.map(_profile_range, jobs):
            stats.update(part_stats)
            for ext, e in part_pri.items():
                pri.setdefault(ext, e)
            for k, rows in part_other.items():
                merged.setdefault(k, []).extend(rows)
    stats["candidates_priority"] = len(pri)
    kept = dict(pri)
    seen = set(kept)
    for type_key, rows in merged.items():
        rows.sort()
        n = 0
        for _, ext, entry in rows:
            if ext in seen:
                continue
            seen.add(ext)
            kept[ext] = entry
            n += 1
            if n >= PER_TYPE_CANDIDATES:
                break
    stats["candidates_for_review_pass"] = len(kept)
    return kept, stats


def images_from(record: dict) -> list[str]:
    """The listing's own image URLs (large size, https, no GIF placeholders), MAIN first."""
    images = record.get("images") or []
    if isinstance(images, dict):  # some dumps store {"large": [...], ...}
        images = [dict(zip(images, v)) for v in zip(*images.values())] if images else []
    urls = []
    first = pick_url(images) if images else None
    if first:
        urls.append(first)
    for im in images:
        for size in ("large", "hi_res"):
            url = im.get(size) if isinstance(im, dict) else None
            if isinstance(url, str) and url.startswith("https://") and not url.lower().endswith(".gif") \
                    and url not in urls:
                urls.append(url)
                break
    return urls[: 1 + MAX_ALTERNATE_IMAGES]


def image_candidates(meta_path: Path, wanted: set[str]) -> dict[str, list[str]]:
    out = {}
    for rec in collect(meta_path, wanted, use_regex=True):
        ext = str(rec.get("parent_asin") or "").strip()
        if ext in wanted and ext not in out:
            out[ext] = images_from(rec)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--base", required=True, help="validated base catalogue directory (selection-v3)")
    parser.add_argument("--selection", default=str(PROJECT / "config" / "selection_amazon2023_v4_additions.toml"))
    parser.add_argument("--base-selection", default=str(PROJECT / "config" / "selection_amazon2023_v3.toml"))
    parser.add_argument("--ranges", default=str(PROJECT / "config" / "price_estimate_ranges.toml"))
    parser.add_argument("--workers", type=int, default=6, help="processes for the metadata pass")
    parser.add_argument("--cache-key", default=None,
                        help="reuse/write per-section profiles in data/experiments/_profile_cache (same inputs only)")
    args = parser.parse_args()

    run_dir = EXPERIMENTS / args.run_id
    if run_dir.exists():
        sys.exit(f"REFUSED: {run_dir} already exists; choose a new --run-id")
    base_dir = Path(args.base)
    base_db = base_dir / "shelfsense.db"
    base_sha_before = file_sha256(base_db)
    run_dir.mkdir(parents=True)
    db_path = run_dir / "shelfsense.db"
    t0 = time.monotonic()
    built_at = datetime.now(timezone.utc).isoformat()

    cfg = load_selection_config(Path(args.selection))
    base_rules = load_selection_config(Path(args.base_selection)).categories["fashion"]
    ranges = load_range_config(Path(args.ranges))
    fx_rates = load_fx_rates(DEFAULT_FX_CONFIG)
    base_meta_path = verified_path(BASE_META)
    source = json.loads((ROOT / "SOURCE.json").read_text(encoding="utf-8"))
    sections = [name for name in SOURCES if name in cfg.categories]
    verified_path(SOURCES[sections[0]][0])  # the first section's files must already be verified

    log("1/7 copying the base catalogue (read-only backup)")
    sqlite_backup(base_db, db_path)
    conn = sqlite3.connect(db_path)
    base_ids, base_titles, base_near = base_keys(conn)
    base_count = len(base_ids)
    # The existing catalogue by its own (v3) types and by the v4 allocation buckets it would fall in.
    group_of = {t.key: t.group for r in cfg.categories.values() for t in r.types}
    base_by_v3_type, base_by_v4_group = Counter(), Counter()
    for (title,) in conn.execute("SELECT title FROM products"):
        base_by_v3_type[base_rules.type_for(title) or "untyped"] += 1
        hit = next((group_of[t] for r in cfg.categories.values() for t in [r.type_for(title)] if t), None)
        base_by_v4_group[hit or "none"] += 1
    conn.close()
    log(f"base: {base_count:,} products; already in the v4 buckets: {dict(base_by_v4_group)}")

    from src.db.connection import get_connection

    chosen: set[str] = set()
    type_of: dict[str, str] = {}
    section_of: dict[str, str] = {}
    add_images: dict[str, list[str]] = {}
    seen_ids, seen_titles, seen_near = set(base_ids), set(base_titles), set(base_near)
    per_section: dict[str, dict] = {}
    reports = {}
    for name in sections:
        rules = cfg.categories[name]
        meta_name, review_name = SOURCES[name]
        meta_path, review_path = wait_verified(meta_name), wait_verified(review_name)
        source = json.loads((ROOT / "SOURCE.json").read_text(encoding="utf-8"))
        adapter = AmazonReviews2023Adapter(
            revision=REVISION, files=((meta_name, FILES[meta_name][1]), (review_name, FILES[review_name][1])),
            retrieved_at=source["files"][meta_name]["retrieved_at"])

        cache = PROFILE_CACHE / f"{args.cache_key}-{name}.json" if args.cache_key else None
        if cache is not None and cache.exists():
            cached = json.loads(cache.read_text(encoding="utf-8"))
            cand = {ext: tuple(e) for ext, e in cached["candidates"].items() if ext not in seen_ids}
            q_reviews, meta_stats, review_stats = cached["q_reviews"], Counter(cached["meta_stats"]), cached["review_stats"]
            log(f"2-3/7 [{name}] profile loaded from {cache}: {len(cand):,} candidates")
        else:
            log(f"2/7 [{name}] profiling metadata ({args.workers} worker processes)")
            cand, meta_stats = profile_additions_parallel(meta_path, rules, REVISION, seen_ids,
                                                          cfg.min_qualifying_reviews, args.workers)
            log(f"[{name}] {len(cand):,} candidates for the review pass")

            log(f"3/7 [{name}] counting qualifying reviews")
            candidates = {ext: Candidate(entry[0], entry[1]) for ext, entry in cand.items()}
            review_stats = profile_reviews(review_path, candidates, set(candidates), adapter, cfg.min_review_chars)
            q_reviews = {ext: c.qualifying for ext, c in candidates.items()}
            del candidates
            if cache is not None:
                PROFILE_CACHE.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps({"candidates": cand, "q_reviews": q_reviews, "meta_stats": dict(meta_stats),
                                             "review_stats": review_stats}), encoding="utf-8")
        qualifying = {ext: e for ext, e in cand.items() if q_reviews[ext] >= cfg.min_qualifying_reviews}
        log(f"[{name}] {len(qualifying):,} qualifying candidates")

        log(f"4/7 [{name}] deduplication and selection")
        unique, dedup_stats = dedupe(qualifying, q_reviews, seen_titles, seen_near)
        capacity = Counter(e[0] for e in unique.values())
        alloc = allocate_groups(rules, dict(capacity)) if rules.groups else \
            allocate_with_priority(rules.quota, dict(capacity), rules.types)
        by_type: dict[str, list] = {}
        for ext, e in unique.items():
            by_type.setdefault(e[0], []).append((-q_reviews[ext], ext))
        picked = {k: [ext for _, ext in sorted(v)[: alloc.get(k, 0)]] for k, v in by_type.items()}
        section_chosen = {ext for ids in picked.values() for ext in ids}
        for k, ids in picked.items():
            for ext in ids:
                type_of[ext], section_of[ext] = k, name
                seen_titles.add(unique[ext][1])
                if unique[ext][2]:
                    seen_near.add(unique[ext][2])
        seen_ids |= set(cand)  # every candidate seen here is skipped in later sections
        chosen |= section_chosen
        by_group = Counter(group_of.get(type_of[ext]) for ext in section_chosen)
        log(f"[{name}] selected {len(section_chosen):,}: " + ", ".join(f"{g} {n}" for g, n in sorted(by_group.items())))

        log(f"5/7 [{name}] ingesting")
        meta_records = collect(meta_path, section_chosen, use_regex=True)
        review_records = collect_reviews_bounded(review_path, section_chosen, adapter,
                                                 cfg.max_reviews_per_product, cfg.min_review_chars)
        for rec in meta_records:
            ext = str(rec.get("parent_asin") or "").strip()
            if ext in section_chosen and ext not in add_images:
                add_images[ext] = images_from(rec)
        conn = get_connection(db_path)
        report = ingest(conn, adapter, meta_records, review_records,
                        max_reviews_per_product=cfg.max_reviews_per_product, min_review_chars=cfg.min_review_chars)
        conn.close()
        del meta_records, review_records
        reports[name] = report.to_dict()
        log(f"[{name}] inserted {report.products_inserted:,} products, {report.reviews_inserted:,} reviews")
        per_section[name] = {
            "files": {meta_name: FILES[meta_name][1], review_name: FILES[review_name][1]},
            "profiling": {"metadata": dict(meta_stats), "reviews": review_stats, "qualifying": len(qualifying)},
            "deduplication": dict(dedup_stats), "capacity_after_dedup": dict(capacity), "allocation": alloc,
            "selected_by_type": {k: len(v) for k, v in picked.items() if v},
            "selected_by_group": dict(by_group),
            "group_quotas": dict(rules.groups),
        }

    conn = get_connection(db_path)
    all_files = [BASE_META, BASE_REVIEWS] + [f for name in sections for f in SOURCES[name]]
    conn.execute("UPDATE data_sources SET notes = ? WHERE name = ?", (
        "Amazon Reviews 2023, revision " + REVISION + ". Files: " + ", ".join(
            f"{k} (sha256 {FILES[k][1]})" for k in all_files), AmazonReviews2023Adapter(revision=REVISION)
        .source_info().name))
    conn.commit()

    log("6/7 estimates, provenance, normalised INR prices, types, images")
    est = generate_estimates(conn, ranges)
    conn.executescript(EXTRA_SCHEMA)
    dataset_version = args.run_id
    rows = conn.execute("SELECT id, external_id, title, price, currency FROM products").fetchall()
    for pid, ext, title, price, currency in rows:
        added = ext in chosen
        meta_file = SOURCES[section_of[ext]][0] if added else BASE_META
        conn.execute("INSERT INTO product_provenance VALUES (?, ?, ?, ?, ?, ?)", (
            pid, meta_file, FILES[meta_file][1], cfg.version if added else "selection-v3", dataset_version, built_at))
        conn.execute("INSERT INTO catalogue_types VALUES (?, ?, ?)", (
            pid, type_of[ext] if added else (base_rules.type_for(title) or "untyped"),
            cfg.version if added else "selection-v3"))
        view = classify_price(price, currency, None, estimates_enabled=False, rates=fx_rates)
        status = {"verified": "verified", "converted": "converted"}.get(view.status, "unavailable")
        fx = view.fx
        conn.execute("INSERT INTO price_normalization VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            pid, price, currency, view.amount, status, view.reason,
            fx.rate if fx else None, fx.rate_date if fx else None, fx.source if fx else None,
            fx.version if fx else None))
    conn.commit()

    base_images = image_candidates(base_meta_path, base_ids)
    images = {}
    ext_to_id = {ext: pid for pid, ext, *_ in rows}
    for ext, urls in list(base_images.items()) + list(add_images.items()):
        if urls and ext in ext_to_id:
            images[str(ext_to_id[ext])] = {"external_id": ext, "url": urls[0], "alternates": urls[1:]}
    (run_dir / "product_image_candidates.json").write_text(json.dumps({
        "source": {"dataset": source["dataset"], "revision": REVISION,
                   "files": {f: FILES[f][1] for f in [BASE_META] + [SOURCES[n][0] for n in sections]},
                   "rule": "MAIN variant large image first, then the listing's other images (https, no GIF placeholders)",
                   "license_status": source["license_status"], "validated": False},
        "images": images}, ensure_ascii=False), encoding="utf-8")

    log("7/7 validation")
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    expected = base_count + len(chosen)
    facts = {
        "products": q("SELECT COUNT(*) FROM products"),
        "reviews": q("SELECT COUNT(*) FROM reviews"),
        "products_fts": q("SELECT COUNT(*) FROM products_fts"),
        "reviews_fts": q("SELECT COUNT(*) FROM reviews_fts"),
        "orphan_reviews": q("SELECT COUNT(*) FROM reviews r LEFT JOIN products p ON p.id = r.product_id WHERE p.id IS NULL"),
        "duplicate_product_ids": q("SELECT COUNT(*) - COUNT(DISTINCT external_id) FROM products"),
        "duplicate_review_ids": q("SELECT COUNT(*) - COUNT(DISTINCT external_id) FROM reviews"),
        "duplicate_titles": q("SELECT COUNT(*) - COUNT(DISTINCT LOWER(title)) FROM products"),
        "empty_titles": q("SELECT COUNT(*) FROM products WHERE title IS NULL OR TRIM(title) = ''"),
        "max_reviews_per_product": q("SELECT MAX(n) FROM (SELECT COUNT(*) n FROM reviews GROUP BY product_id)"),
        "min_reviews_per_product": q("SELECT MIN(n) FROM (SELECT COUNT(*) n FROM reviews GROUP BY product_id)"),
        "products_without_reviews": q("SELECT COUNT(*) FROM products p WHERE NOT EXISTS (SELECT 1 FROM reviews r WHERE r.product_id = p.id)"),
        "short_reviews": q("SELECT COUNT(*) FROM reviews WHERE LENGTH(text) < 20"),
        "currencies": dict(conn.execute("SELECT COALESCE(currency, 'none'), COUNT(*) FROM products GROUP BY 1").fetchall()),
        "invalid_source_prices": q("SELECT COUNT(*) FROM products WHERE price IS NOT NULL AND price <= 0"),
        "estimates": q("SELECT COUNT(*) FROM price_estimates"),
        "estimates_on_priced_products": q("SELECT COUNT(*) FROM price_estimates e JOIN products p ON p.id = e.product_id WHERE p.price IS NOT NULL"),
        "provenance_rows": q("SELECT COUNT(*) FROM product_provenance"),
        "type_rows": q("SELECT COUNT(*) FROM catalogue_types"),
        "price_normalization_rows": q("SELECT COUNT(*) FROM price_normalization"),
        "price_status": dict(conn.execute("SELECT status, COUNT(*) FROM price_normalization GROUP BY 1").fetchall()),
        "missing_brand": q("SELECT COUNT(*) FROM products WHERE brand_id IS NULL"),
        "missing_description": q("SELECT COUNT(*) FROM products WHERE description IS NULL"),
        "missing_rating": q("SELECT COUNT(*) FROM products WHERE avg_rating IS NULL"),
        "personal_data_markers": q("SELECT COUNT(*) FROM products WHERE attributes_json LIKE '%user_id%'"),
        "integrity_check": q("PRAGMA integrity_check"),
        "schema_version": q("SELECT MAX(version) FROM schema_version"),
        "experimental_v3_marker": q("SELECT COUNT(*) FROM schema_version WHERE version = 3"),
        "products_with_image_candidates": len(images),
    }
    problems = []
    if facts["products"] != expected:
        problems.append(f"{facts['products']} products, expected {expected}")
    for key in ("orphan_reviews", "duplicate_product_ids", "duplicate_review_ids", "empty_titles", "short_reviews",
                "products_without_reviews", "estimates_on_priced_products", "personal_data_markers"):
        if facts[key]:
            problems.append(f"{key} = {facts[key]}")
    if facts["products_fts"] != facts["products"] or facts["reviews_fts"] != facts["reviews"]:
        problems.append("FTS row counts differ from table counts")
    if (facts["max_reviews_per_product"] > cfg.max_reviews_per_product
            or facts["min_reviews_per_product"] < cfg.min_qualifying_reviews):
        problems.append(f"reviews per product outside {cfg.min_qualifying_reviews}..{cfg.max_reviews_per_product}: "
                        f"{facts['min_reviews_per_product']}..{facts['max_reviews_per_product']}")
    for key in ("provenance_rows", "type_rows", "price_normalization_rows"):
        if facts[key] != facts["products"]:
            problems.append(f"{key} = {facts[key]} != products")
    if (facts["integrity_check"] != "ok" or facts["schema_version"] != SCHEMA_VERSION
            or facts["experimental_v3_marker"] != 1):
        problems.append(f"integrity {facts['integrity_check']}, schema v{facts['schema_version']}")
    if file_sha256(base_db) != base_sha_before:
        problems.append("the base catalogue database changed during the build")

    by_type = dict(conn.execute("SELECT type_key, COUNT(*) FROM catalogue_types GROUP BY 1 ORDER BY 2 DESC").fetchall())
    added_by_type = dict(Counter(type_of.values()))
    added_by_group = Counter(group_of.get(t) for t in type_of.values())
    allocation_table = {g: {"requested": q, "added": added_by_group.get(g, 0),
                            "shortfall": max(0, q - added_by_group.get(g, 0)),
                            "already_in_base": base_by_v4_group.get(g, 0)}
                        for r in cfg.categories.values() for g, q in r.groups}
    cov = coverage(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    manifest = {
        "dataset_version": dataset_version,
        "built_at": built_at,
        "seconds": round(time.monotonic() - t0),
        "target_products": 100_000,
        "products": facts["products"],
        "target_reached": facts["products"] >= 100_000,
        "database": _rel(db_path),
        "database_sha256": file_sha256(db_path),
        "base": {"directory": str(base_dir), "database_sha256": base_sha_before, "products": base_count,
                 "unchanged": file_sha256(base_db) == base_sha_before},
        "source": {"dataset": source["dataset"], "revision": REVISION, "license_status": source["license_status"],
                   "files": {k: {"sha256": v["verified_sha256"], "bytes": v["bytes"], "retrieved_at": v["retrieved_at"]}
                             for k, v in source["files"].items()
                             if k in (BASE_META, "raw/review_categories/Amazon_Fashion.jsonl", ADD_META, ADD_REVIEWS)}},
        "selection": {"version": cfg.version, "sha256": cfg.sha256,
                      "quota": sum(r.quota for r in cfg.categories.values()),
                      "per_type_candidate_cap": PER_TYPE_CANDIDATES, "added": len(chosen),
                      "allocation": allocation_table, "added_by_type": added_by_type,
                      "min_qualifying_reviews": cfg.min_qualifying_reviews, "min_review_chars": cfg.min_review_chars,
                      "max_reviews_per_product": cfg.max_reviews_per_product},
        "base_inspection": {"by_v3_type": dict(base_by_v3_type.most_common()),
                            "by_v4_bucket": dict(base_by_v4_group.most_common())},
        "sections": per_section,
        "deduplication": {k: sum(per_section[n]["deduplication"].get(k, 0) for n in sections)
                          for k in ("identical_title_in_base", "near_duplicate_in_base",
                                    "identical_title_within_additions", "near_duplicate_within_additions")},
        "ingest": reports,
        "categories": by_type,
        "prices": {"fx_config": str(DEFAULT_FX_CONFIG.relative_to(PROJECT)), "fx_config_sha256": fx_config_sha256(),
                   "fx_version": next(iter(fx_rates.values())).version if fx_rates else None,
                   "status": facts["price_status"], "estimates": est.estimated,
                   "estimate_config": {"version": ranges.version, "sha256": ranges.sha256}},
        "images": {"candidates_file": "product_image_candidates.json",
                   "products_with_candidates": len(images), "validated": False},
        "coverage": cov,
        "validation": {"facts": facts, "problems": problems},
    }
    (run_dir / "dataset_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"wrote {run_dir / 'dataset_manifest.json'}")
    if problems:
        print("CRITICAL VALIDATION FAILED:", *problems, sep="\n  ", file=sys.stderr)
        sys.exit(1)
    log(f"all critical validation checks passed: {facts['products']:,} products, {facts['reviews']:,} reviews")


if __name__ == "__main__":
    main()
