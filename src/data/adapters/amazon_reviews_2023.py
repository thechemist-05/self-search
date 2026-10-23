"""Adapter for Amazon Reviews 2023 (McAuley Lab, UCSD) raw JSONL records.

Rights (audited 2026-10-21): the dataset states no licence. It is used only for
local academic prototyping, with attribution; raw files are not redistributed.

Products: ``parent_asin`` is the product id. A price is kept only when the source
gives a plain positive number, and it is labelled USD (amazon.com, at crawl time).
It is never converted. Brand comes only from ``details["Brand"]``; ``store`` is a
storefront name, kept as an attribute, not a brand.

Reviews: linked by the record's own ``parent_asin`` (exact match happens in the
pipeline). The dataset has no review id, so the id is a hash of the review's own
fields. ``user_id`` and review images are dropped here.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional

from src.data.adapters.base import (
    ProductRecord,
    ProductResult,
    Rejection,
    ReviewRecord,
    ReviewResult,
    SourceAdapter,
    SourceInfo,
)
from src.data.validation import clean_text

SOURCE_NAME = "amazon-reviews-2023"
SOURCE_URL = "https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023"
LICENSE_STATUS = ("No licence stated by the dataset (audited 2026-10-21). Local academic prototyping "
                  "only; attribution to McAuley Lab (Hou et al., 2024); raw files not redistributed.")
# Fields deliberately not kept: personal data, media and cross-sell lists.
DROPPED_PRODUCT_FIELDS = ("images", "videos", "bought_together")
DROPPED_REVIEW_FIELDS = ("user_id", "images")
_NUMERIC = re.compile(r"\d+(?:\.\d+)?")


def _ref(record: dict[str, Any], i: int) -> str:
    return f"line {record.get('_line', i)}"


def _rating(value: Any, lo: float) -> tuple[Optional[float], bool]:
    """(rating, ok). None is allowed; anything present must be a number in [lo, 5]."""
    if value is None:
        return None, True
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
        return None, False
    return (float(value), True) if lo <= value <= 5 else (None, False)


def _verified_usd_price(value: Any) -> Optional[float]:
    """A plain positive number only ("from 9.99", ranges, None and 0 are not prices)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 and math.isfinite(value) else None
    if isinstance(value, str) and _NUMERIC.fullmatch(value.strip()):
        number = float(value.strip())
        return number if number > 0 else None
    return None


class AmazonReviews2023Adapter(SourceAdapter):
    """Transform raw metadata and review records of one category file pair."""

    def __init__(self, revision: str, files: tuple[tuple[str, str], ...] = (), retrieved_at: Optional[str] = None):
        self.revision = revision
        self.files = files
        self.retrieved_at = retrieved_at

    def source_info(self) -> SourceInfo:
        return SourceInfo(name=SOURCE_NAME, url=SOURCE_URL, license=LICENSE_STATUS, version=self.revision,
                          retrieved_at=self.retrieved_at, files=self.files,
                          notes="Prices are USD from amazon.com at crawl time (2023); never converted.")

    def parse_products(self, records: Iterable[dict[str, Any]]) -> Iterator[ProductResult]:
        for i, rec in enumerate(records, 1):
            ref = _ref(rec, i)
            if "_invalid" in rec:
                yield Rejection("product", "malformed_record", ref, rec["_invalid"])
                continue
            asin = str(rec.get("parent_asin") or "").strip()
            if not asin:
                yield Rejection("product", "missing_parent_asin", ref)
                continue
            title = clean_text(rec.get("title"))
            if not title:
                yield Rejection("product", "missing_title", asin)
                continue
            avg, ok = _rating(rec.get("average_rating"), 0)
            if not ok:
                yield Rejection("product", "invalid_average_rating", asin, repr(rec.get("average_rating")))
                continue
            count = rec.get("rating_number")
            count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None

            details = rec.get("details") or {}
            if isinstance(details, str):
                try:
                    details = json.loads(details)
                except ValueError:
                    details = {}
            brand = details.get("Brand") if isinstance(details, dict) else None
            brand = clean_text(brand) or None

            raw_price = rec.get("price")
            price = _verified_usd_price(raw_price)
            description = clean_text(" ".join(d for d in rec.get("description") or [] if isinstance(d, str))) \
                if isinstance(rec.get("description"), list) else clean_text(rec.get("description"))
            categories = tuple(c for c in rec.get("categories") or [] if isinstance(c, str) and c.strip())
            main_category = clean_text(rec.get("main_category")) or None
            attributes: dict[str, Any] = {
                "features": [clean_text(f) for f in rec.get("features") or [] if clean_text(f)],
                "details": details if isinstance(details, dict) else {},
                "store": clean_text(rec.get("store")) or None,
                "main_category": main_category,
                "brand_source": "details.Brand" if brand else None,
                "price_basis": "verified_source_usd_amazon_com_2023" if price is not None else None,
            }
            if raw_price not in (None, "", "None") and price is None:
                attributes["price_raw"] = str(raw_price)  # kept for audit, never used as a price
            yield ProductRecord(
                external_id=asin,
                title=title,
                description=description or None,
                category_path=categories or ((main_category,) if main_category else ()),
                brand=brand,
                price=price,
                currency="USD" if price is not None else None,
                avg_rating=avg,
                rating_count=count,
                attributes={k: v for k, v in attributes.items() if v not in (None, [], {})},
                provenance={"source": SOURCE_NAME, "revision": self.revision, "ref": ref},
            )

    def parse_reviews(self, records: Iterable[dict[str, Any]]) -> Iterator[ReviewResult]:
        for i, rec in enumerate(records, 1):
            ref = _ref(rec, i)
            if "_invalid" in rec:
                yield Rejection("review", "malformed_record", ref, rec["_invalid"])
                continue
            asin = str(rec.get("parent_asin") or "").strip()
            if not asin:
                yield Rejection("review", "missing_parent_asin", ref)
                continue
            text = clean_text(rec.get("text"))
            if not text:
                yield Rejection("review", "missing_text", ref)
                continue
            rating, ok = _rating(rec.get("rating"), 1)
            if not ok:
                yield Rejection("review", "invalid_rating", ref, repr(rec.get("rating")))
                continue
            title = clean_text(rec.get("title")) or None
            ts = rec.get("timestamp")
            date = None
            if isinstance(ts, int) and not isinstance(ts, bool) and ts > 0:
                date = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat()
            # No review id in the source: hash the review's own fields (no reviewer data).
            key = "|".join(str(x) for x in (asin, rec.get("asin") or "", ts or "", title or "", text))
            helpful = rec.get("helpful_vote")
            yield ReviewRecord(
                external_id="ar23-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32],
                product_external_id=asin,
                text=text,
                title=title,
                rating=rating,
                review_date=date,
                helpful_votes=helpful if isinstance(helpful, int) and not isinstance(helpful, bool) and helpful > 0 else 0,
                verified=rec.get("verified_purchase") is True,
                provenance={"source": SOURCE_NAME, "revision": self.revision, "ref": ref},
            )
