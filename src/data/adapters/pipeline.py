"""Insert adapter output into an (experimental) database: validated, deduplicated, idempotent.

* Products: deduplicated by the source's id (the first occurrence wins; later ones
  are rejected as ``duplicate_in_source``). A product already stored for this source
  is counted as existing, so re-runs insert nothing. Missing values are inserted as
  NULL explicitly, so the schema default ``currency='USD'`` never applies.
* Reviews: attached only when ``product_external_id`` exactly equals a product id
  of the same source. Nothing is matched by title, similarity or another source.
* An optional per-product cap keeps the best reviews in a fixed order (verified,
  then helpful votes, then newest, then id) and rejects the rest explicitly.
* An optional minimum text length rejects shorter reviews as ``short_text``
  (the selection rules count only reviews of at least that length).
* Categories: the top level of the source path goes in ``categories`` (names are
  unique there, so deeper levels could collide across sources); the full path is
  kept in ``attributes_json["category_path"]``, which FTS indexes.
* Everything, including the ``data_sources`` row, is written in one transaction;
  on any error nothing is kept.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from src.data.adapters.base import ProductRecord, Rejection, ReviewRecord, SourceAdapter
from src.data.validation import validate_product, validate_review

MAX_REJECTION_SAMPLES = 20


@dataclass
class IngestReport:
    """What one ingest run read, inserted, found existing and rejected (with reasons)."""

    source: str
    products_read: int = 0
    products_inserted: int = 0
    products_existing: int = 0
    reviews_read: int = 0
    reviews_inserted: int = 0
    reviews_existing: int = 0
    rejections: Counter = field(default_factory=Counter)
    rejection_samples: dict[str, list[str]] = field(default_factory=dict)

    def reject(self, r: Rejection) -> None:
        key = f"{r.kind}:{r.reason}"
        self.rejections[key] += 1
        samples = self.rejection_samples.setdefault(key, [])
        if len(samples) < MAX_REJECTION_SAMPLES:
            samples.append(r.source_ref + (f" ({r.detail})" if r.detail else ""))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["rejections"] = dict(sorted(self.rejections.items()))
        return data

    def write(self, path: Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Records from a local JSONL file, each tagged with its 1-based ``_line``.

    A line that is not a JSON object is yielded as ``{"_line": n, "_invalid": reason}``
    so the adapter step can reject it with a reason instead of crashing.
    """
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                yield {"_line": n, "_invalid": f"invalid JSON: {exc.msg}"}
                continue
            if not isinstance(record, dict):
                yield {"_line": n, "_invalid": "not a JSON object"}
                continue
            record["_line"] = n
            yield record


def _split_invalid(records: Iterable[dict[str, Any]], kind: str, report: IngestReport) -> Iterator[dict[str, Any]]:
    for rec in records:
        if "_invalid" in rec:
            report.reject(Rejection(kind, "malformed_record", f"line {rec['_line']}", rec["_invalid"]))
        else:
            yield rec


def _review_rank(r: ReviewRecord) -> tuple:
    return (not r.verified, -r.helpful_votes, "" if r.review_date is None else _neg_date(r.review_date), r.external_id)


def _neg_date(date: str) -> str:
    # Newest first: invert each digit so a plain ascending sort puts later dates first.
    return "".join(chr(ord("9") - ord(c) + ord("0")) if c.isdigit() else c for c in date)


def _id_for(conn: sqlite3.Connection, table: str, name: str) -> int:
    conn.execute(f"INSERT OR IGNORE INTO {table} (name) VALUES (?)", (name,))
    return conn.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()[0]


def ingest(
    conn: sqlite3.Connection,
    adapter: SourceAdapter,
    product_records: Iterable[dict[str, Any]],
    review_records: Iterable[dict[str, Any]] = (),
    *,
    max_reviews_per_product: Optional[int] = None,
    min_review_chars: Optional[int] = None,
) -> IngestReport:
    """Ingest one source's records. Re-running with the same input inserts nothing."""
    info = adapter.source_info()
    report = IngestReport(source=info.name)
    notes = json.dumps({"version": info.version, "retrieved_at": info.retrieved_at,
                        "files": [{"name": n, "sha256": h} for n, h in info.files], "notes": info.notes})

    # Parse fully before writing, so a crash leaves the database untouched.
    products: list[ProductRecord] = []
    seen: set[str] = set()
    for item in adapter.parse_products(_split_invalid(product_records, "product", report)):
        report.products_read += 1
        if isinstance(item, Rejection):
            report.reject(item)
            continue
        problems = validate_product({"title": item.title, "external_id": item.external_id,
                                     "price": item.price, "avg_rating": item.avg_rating})
        if not problems.is_valid:
            report.reject(Rejection("product", "failed_validation", item.external_id, "; ".join(problems.errors)))
        elif item.external_id in seen:
            report.reject(Rejection("product", "duplicate_in_source", item.external_id))
        else:
            seen.add(item.external_id)
            products.append(item)

    reviews: list[ReviewRecord] = []
    seen_reviews: set[str] = set()
    for item in adapter.parse_reviews(_split_invalid(review_records, "review", report)):
        report.reviews_read += 1
        if isinstance(item, Rejection):
            report.reject(item)
        elif item.external_id in seen_reviews:
            report.reject(Rejection("review", "duplicate_in_source", item.external_id))
        else:
            seen_reviews.add(item.external_id)
            reviews.append(item)

    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN")
    try:
        row = conn.execute("SELECT id FROM data_sources WHERE name = ?", (info.name,)).fetchone()
        if row is None:
            source_id = conn.execute(
                "INSERT INTO data_sources (name, url, license, downloaded_at, notes) VALUES (?, ?, ?, ?, ?)",
                (info.name, info.url, info.license, info.retrieved_at, notes),
            ).lastrowid
        else:
            source_id = row[0]
        existing = {row[0]: row[1] for row in conn.execute(
            "SELECT external_id, id FROM products WHERE source_id = ?", (source_id,))}
        for p in products:
            if p.external_id in existing:
                report.products_existing += 1
                continue
            category_id = _id_for(conn, "categories", p.category_path[0]) if p.category_path else None
            brand_id = _id_for(conn, "brands", p.brand) if p.brand else None
            attributes = {**p.attributes, "provenance": p.provenance}
            if p.category_path:
                attributes["category_path"] = list(p.category_path)
            cur = conn.execute(
                "INSERT INTO products (source_id, external_id, title, description, category_id, brand_id, "
                "price, currency, avg_rating, rating_count, image_url, attributes_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (source_id, p.external_id, p.title, p.description, category_id, brand_id,
                 p.price, p.currency, p.avg_rating, p.rating_count, json.dumps(attributes, ensure_ascii=False)),
            )
            existing[p.external_id] = cur.lastrowid
            report.products_inserted += 1

        # Exact identifier match only, then the per-product cap.
        by_product: dict[str, list[ReviewRecord]] = {}
        for r in reviews:
            if r.product_external_id not in existing:
                report.reject(Rejection("review", "no_matching_product", r.external_id, r.product_external_id))
                continue
            check = validate_review({"text": r.text, "product_id": existing[r.product_external_id], "rating": r.rating})
            if not check.is_valid:
                report.reject(Rejection("review", "failed_validation", r.external_id, "; ".join(check.errors)))
                continue
            if min_review_chars is not None and len(r.text) < min_review_chars:
                report.reject(Rejection("review", "short_text", r.external_id, f"{len(r.text)} chars"))
                continue
            by_product.setdefault(r.product_external_id, []).append(r)
        stored = {row[0] for row in conn.execute("SELECT external_id FROM reviews WHERE source_id = ?", (source_id,))}
        for ext_pid, group in sorted(by_product.items()):
            group.sort(key=_review_rank)
            keep = group if max_reviews_per_product is None else group[:max_reviews_per_product]
            for r in group[len(keep):]:
                report.reject(Rejection("review", "over_per_product_cap", r.external_id, ext_pid))
            for r in keep:
                if r.external_id in stored:
                    report.reviews_existing += 1
                    continue
                conn.execute(
                    "INSERT INTO reviews (product_id, source_id, external_id, rating, title, text, review_date, "
                    "helpful_votes, verified_flag) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (existing[ext_pid], source_id, r.external_id, r.rating, r.title, r.text, r.review_date,
                     r.helpful_votes, 1 if r.verified else 0),
                )
                report.reviews_inserted += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return report
