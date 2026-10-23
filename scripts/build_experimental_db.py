#!/usr/bin/env python
"""Build the experimental Fashion catalogue database from verified Amazon Reviews 2023 files.

    python scripts/build_experimental_db.py --run-id fashion-v2-<ts>

Writes only to ``data/experiments/<run-id>/`` (refuses an existing run directory):
``shelfsense.db`` (current schema + experimental v3 marker) and ``ingest_report.json``.

Steps: verified files only -> selection (same code as the profiler) -> adapter +
pipeline (exact parent_asin matching, dedup, <=8 reviews of >=20 chars per product,
reviewer ids dropped) -> synthetic INR estimates for products without a source price
(stored in ``price_estimates`` only) -> critical validation. Exit code 1 on any
critical validation failure; the production database is never opened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.fetch_amazon2023 import FILES, REVISION, ROOT  # noqa: E402
from scripts.profile_amazon2023 import (  # noqa: E402
    CATEGORY_FILES, PARENT_RE, log, profile_meta, profile_reviews, select_products, verified_path,
)
from src.data.adapters.amazon_reviews_2023 import AmazonReviews2023Adapter  # noqa: E402
from src.data.adapters.pipeline import ingest  # noqa: E402
from src.data.selection import load_selection_config  # noqa: E402
from src.db.connection import get_connection  # noqa: E402
from src.db.experimental_schema import apply_experimental_v3  # noqa: E402
from src.db.schema import SCHEMA_VERSION  # noqa: E402
from src.db.migrations import run_migrations  # noqa: E402
from src.pricing.estimates import generate_estimates, load_range_config  # noqa: E402

PROJECT = Path(__file__).resolve().parents[1]
EXPERIMENTS = PROJECT / "data" / "experiments"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def collect(path: Path, wanted: set[str], use_regex: bool) -> list[dict]:
    """Raw records of the wanted products, each tagged with its line number."""
    out = []
    with open(path, "rb") as fh:
        for n, raw in enumerate(fh, 1):
            if use_regex:
                m = PARENT_RE.search(raw)
                if not m or m.group(1).decode("utf-8", "replace").strip() not in wanted:
                    continue
            rec = json.loads(raw)
            if not use_regex and str(rec.get("parent_asin") or "").strip() not in wanted:
                continue
            rec["_line"] = n
            out.append(rec)
    return out


def validate(conn: sqlite3.Connection, expected_products: int) -> tuple[dict, list[str]]:
    """Critical checks on the built database. Returns (facts, problems)."""
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    facts = {
        "products": q("SELECT COUNT(*) FROM products"),
        "reviews": q("SELECT COUNT(*) FROM reviews"),
        "products_fts": q("SELECT COUNT(*) FROM products_fts"),
        "reviews_fts": q("SELECT COUNT(*) FROM reviews_fts"),
        "orphan_reviews": q("SELECT COUNT(*) FROM reviews r LEFT JOIN products p ON p.id = r.product_id "
                            "WHERE p.id IS NULL"),
        "review_source_mismatch": q("SELECT COUNT(*) FROM reviews r JOIN products p ON p.id = r.product_id "
                                    "WHERE r.source_id != p.source_id"),
        "duplicate_product_ids": q("SELECT COUNT(*) - COUNT(DISTINCT external_id) FROM products"),
        "duplicate_review_ids": q("SELECT COUNT(*) - COUNT(DISTINCT external_id) FROM reviews"),
        "max_reviews_per_product": q("SELECT COALESCE(MAX(n), 0) FROM (SELECT COUNT(*) n FROM reviews GROUP BY product_id)"),
        "min_reviews_per_product": q("SELECT COALESCE(MIN(n), 0) FROM (SELECT COUNT(*) n FROM reviews GROUP BY product_id)"),
        "short_reviews": q("SELECT COUNT(*) FROM reviews WHERE LENGTH(text) < 20"),
        "currencies": dict(conn.execute("SELECT COALESCE(currency, 'none'), COUNT(*) FROM products GROUP BY 1").fetchall()),
        "estimates": q("SELECT COUNT(*) FROM price_estimates"),
        "estimates_on_priced_products": q("SELECT COUNT(*) FROM price_estimates e JOIN products p "
                                          "ON p.id = e.product_id WHERE p.price IS NOT NULL"),
        "personal_data_markers": q("SELECT COUNT(*) FROM products WHERE attributes_json LIKE '%user_id%'"),
        "integrity_check": q("PRAGMA integrity_check"),
        "schema_version": q("SELECT MAX(version) FROM schema_version"),
        "experimental_v3_marker": q("SELECT COUNT(*) FROM schema_version WHERE version = 3"),
    }
    problems = []
    if facts["products"] != expected_products:
        problems.append(f"inserted {facts['products']} products, selected {expected_products}")
    for key in ("orphan_reviews", "review_source_mismatch", "duplicate_product_ids", "duplicate_review_ids",
                "short_reviews", "estimates_on_priced_products", "personal_data_markers"):
        if facts[key]:
            problems.append(f"{key} = {facts[key]}")
    if facts["products_fts"] != facts["products"] or facts["reviews_fts"] != facts["reviews"]:
        problems.append("FTS row counts differ from table counts")
    if facts["max_reviews_per_product"] > 8 or facts["min_reviews_per_product"] < 3:
        problems.append(f"reviews per product outside 3..8: {facts['min_reviews_per_product']}..{facts['max_reviews_per_product']}")
    if set(facts["currencies"]) - {"USD", "none"}:
        problems.append(f"unexpected currencies {facts['currencies']}")
    if (facts["integrity_check"] != "ok" or facts["schema_version"] != SCHEMA_VERSION
            or facts["experimental_v3_marker"] != 1):
        problems.append(f"integrity {facts['integrity_check']}, schema v{facts['schema_version']}")
    return facts, problems


def coverage(conn: sqlite3.Connection) -> dict:
    n = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] or 1
    pct = lambda sql: round(100 * conn.execute(sql).fetchone()[0] / n, 1)  # noqa: E731
    return {
        "description_pct": pct("SELECT COUNT(*) FROM products WHERE description IS NOT NULL"),
        "brand_pct": pct("SELECT COUNT(*) FROM products WHERE brand_id IS NOT NULL"),
        "usd_price_pct": pct("SELECT COUNT(*) FROM products WHERE price IS NOT NULL"),
        "avg_rating_pct": pct("SELECT COUNT(*) FROM products WHERE avg_rating IS NOT NULL"),
        "estimated_inr_pct": pct("SELECT COUNT(*) FROM price_estimates"),
        "verified_review_pct": round(100 * conn.execute("SELECT AVG(verified_flag) FROM reviews").fetchone()[0], 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--category", default="fashion", choices=sorted(CATEGORY_FILES))
    parser.add_argument("--selection", default=str(PROJECT / "config" / "selection_amazon2023_v2.toml"))
    parser.add_argument("--ranges", default=str(PROJECT / "config" / "price_estimate_ranges.toml"))
    args = parser.parse_args()

    run_dir = EXPERIMENTS / args.run_id
    if run_dir.exists():
        sys.exit(f"REFUSED: {run_dir} already exists; choose a new --run-id")
    run_dir.mkdir(parents=True)
    db_path = run_dir / "shelfsense.db"
    t0 = time.monotonic()

    cfg = load_selection_config(Path(args.selection))
    rules = cfg.categories[args.category]
    ranges = load_range_config(Path(args.ranges))
    meta_name, review_name = CATEGORY_FILES[args.category]
    meta_path, review_path = verified_path(meta_name), verified_path(review_name)
    source = json.loads((ROOT / "SOURCE.json").read_text(encoding="utf-8"))
    adapter = AmazonReviews2023Adapter(
        revision=REVISION,
        files=((meta_name, FILES[meta_name][1]), (review_name, FILES[review_name][1])),
        retrieved_at=source["files"][meta_name]["retrieved_at"],
    )

    log("selection pass 1/2: products")
    _, candidates, all_ids = profile_meta(meta_path, rules, adapter)
    log("selection pass 2/2: reviews")
    profile_reviews(review_path, candidates, all_ids, adapter, cfg.min_review_chars)
    qualifying, capacity, alloc, selected = select_products(candidates, cfg, rules)
    chosen = {pid for ids in selected.values() for pid in ids}
    expected_reviews = sum(min(candidates[p].qualifying, cfg.max_reviews_per_product) for p in chosen)
    type_of = {pid: k for k, ids in selected.items() for pid in ids}
    del candidates, all_ids, qualifying
    log(f"selected {len(chosen):,} products, profiled reviews {expected_reviews:,}")

    meta_records = collect(meta_path, chosen, use_regex=False)
    review_records = collect(review_path, chosen, use_regex=True)
    log(f"collected {len(meta_records):,} product and {len(review_records):,} review records")

    conn = get_connection(db_path)
    run_migrations(conn)
    apply_experimental_v3(conn)
    report = ingest(conn, adapter, meta_records, review_records,
                    max_reviews_per_product=cfg.max_reviews_per_product, min_review_chars=cfg.min_review_chars)
    log(f"inserted {report.products_inserted:,} products, {report.reviews_inserted:,} reviews")
    est = generate_estimates(conn, ranges)
    log(f"estimates: {est.estimated:,} written, skipped {est.skipped}")

    facts, problems = validate(conn, len(chosen))
    by_type = {}
    for (ext,) in conn.execute("SELECT external_id FROM products"):
        by_type[type_of[ext]] = by_type.get(type_of[ext], 0) + 1
    reviews_by_type = {}
    for ext, n in conn.execute("SELECT p.external_id, COUNT(*) FROM reviews r JOIN products p "
                               "ON p.id = r.product_id GROUP BY p.id"):
        reviews_by_type[type_of[ext]] = reviews_by_type.get(type_of[ext], 0) + n
    cov = coverage(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    result = {
        "run_id": args.run_id,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "seconds": round(time.monotonic() - t0),
        "database": str(db_path.relative_to(PROJECT)),
        "database_bytes": db_path.stat().st_size,
        "database_sha256": file_sha256(db_path),
        "source": {"dataset": source["dataset"], "revision": REVISION, "license_status": source["license_status"],
                   "files": {k: {"sha256": v["verified_sha256"], "bytes": v["bytes"], "retrieved_at": v["retrieved_at"]}
                             for k, v in source["files"].items()}},
        "selection": {"version": cfg.version, "sha256": cfg.sha256, "quota": rules.quota,
                      "selected_products": len(chosen), "profiled_reviews": expected_reviews,
                      "allocation": alloc},
        "price_estimates": {"config_version": ranges.version, "config_sha256": ranges.sha256,
                            "estimated": est.estimated, "skipped": est.skipped,
                            "note": "Illustrative synthetic INR estimates; disabled by default when serving."},
        "ingest": report.to_dict(),
        "validation": {"facts": facts, "problems": problems},
        "coverage": cov,
        "products_by_type": dict(sorted(by_type.items())),
        "reviews_by_type": dict(sorted(reviews_by_type.items())),
    }
    (run_dir / "ingest_report.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"wrote {run_dir / 'ingest_report.json'}")
    if problems:
        print("CRITICAL VALIDATION FAILED:", *problems, sep="\n  ", file=sys.stderr)
        sys.exit(1)
    log("all critical validation checks passed")


if __name__ == "__main__":
    main()
