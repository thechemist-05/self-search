"""Adapter for Amazon Berkeley Objects (ABO) listings: amazon.in, catalogue only.

Licence: CC BY 4.0. Credit for the data goes to Amazon.com; credit for building the
dataset goes to Matthieu Guillaumin, Thomas Dideriksen, Kenan Deng, Himanshu Arora
(Amazon.com), Jasmine Collins and Jitendra Malik (UC Berkeley).

ABO has no prices, ratings or reviews, so those stay None. Only amazon.in listings
are accepted. Titles are taken in English as written (en_IN first); nothing is
translated. ABO records are never linked to Amazon Reviews 2023 records.
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator, Optional

from src.data.adapters.base import ProductRecord, ProductResult, Rejection, SourceAdapter, SourceInfo
from src.data.validation import clean_text

SOURCE_NAME = "abo-amazon-in"
SOURCE_URL = "https://amazon-berkeley-objects.s3.amazonaws.com/index.html"
LICENSE = "CC BY 4.0"
ATTRIBUTION = ("Data: Amazon.com. Dataset: Matthieu Guillaumin, Thomas Dideriksen, Kenan Deng, "
               "Himanshu Arora (Amazon.com), Jasmine Collins and Jitendra Malik (UC Berkeley).")
MARKETPLACE = "amazon.in"
_TEXT_FIELDS = ("color", "material", "fabric_type", "style", "pattern", "model_name", "finish_type", "item_shape")


def _pick(values: Any) -> tuple[Optional[str], Optional[str]]:
    """(value, language tag): en_IN first, then any other English tag; never another language."""
    if not isinstance(values, list):
        return None, None
    entries = [v for v in values if isinstance(v, dict) and clean_text(v.get("value"))]
    for wanted in ("en_IN", None):
        for v in entries:
            tag = str(v.get("language_tag") or "")
            if (tag == wanted) if wanted else tag.startswith("en_"):
                return clean_text(v["value"]), tag
    return None, None


def _all_english(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [clean_text(v["value"]) for v in values
            if isinstance(v, dict) and str(v.get("language_tag") or "").startswith("en_") and clean_text(v.get("value"))]


class AboAdapter(SourceAdapter):
    """Transform ABO listing records (amazon.in only)."""

    def __init__(self, version: str, files: tuple[tuple[str, str], ...] = (), retrieved_at: Optional[str] = None):
        self.version = version
        self.files = files
        self.retrieved_at = retrieved_at

    def source_info(self) -> SourceInfo:
        return SourceInfo(name=SOURCE_NAME, url=SOURCE_URL, license=LICENSE, version=self.version,
                          retrieved_at=self.retrieved_at, files=self.files,
                          notes=f"{ATTRIBUTION} Filtered to {MARKETPLACE}. No prices, ratings or reviews.")

    def parse_products(self, records: Iterable[dict[str, Any]]) -> Iterator[ProductResult]:
        for i, rec in enumerate(records, 1):
            ref = f"line {rec.get('_line', i)}"
            if "_invalid" in rec:
                yield Rejection("product", "malformed_record", ref, rec["_invalid"])
                continue
            item_id = str(rec.get("item_id") or "").strip()
            if not item_id:
                yield Rejection("product", "missing_item_id", ref)
                continue
            if rec.get("domain_name") != MARKETPLACE:
                yield Rejection("product", "out_of_scope_marketplace", item_id, str(rec.get("domain_name")))
                continue
            title, title_tag = _pick(rec.get("item_name"))
            if not title:
                yield Rejection("product", "missing_english_title", item_id)
                continue
            brand, _ = _pick(rec.get("brand"))
            description, _ = _pick(rec.get("product_description"))
            nodes = rec.get("node") or []
            path = next((n.get("path") for n in nodes if isinstance(n, dict) and n.get("path")), None)
            category_path = tuple(p for p in str(path).split("/") if p.strip()) if path else ()
            product_types = [p.get("value") for p in rec.get("product_type") or [] if isinstance(p, dict) and p.get("value")]
            attributes: dict[str, Any] = {
                "marketplace": MARKETPLACE,
                "title_language": title_tag,
                "product_type": product_types,
                "bullet_points": _all_english(rec.get("bullet_point")),
                "item_keywords": _all_english(rec.get("item_keywords")),
                "brand_source": "abo.brand" if brand else None,
                "title_hi": next((clean_text(v["value"]) for v in rec.get("item_name") or []
                                  if isinstance(v, dict) and v.get("language_tag") == "hi_IN"), None),
            }
            for name in _TEXT_FIELDS:
                attributes[name] = _pick(rec.get(name))[0]
            if not category_path and product_types:
                category_path = (product_types[0],)
            yield ProductRecord(
                external_id=item_id,
                title=title,
                description=description or None,
                category_path=category_path,
                brand=brand,
                attributes={k: v for k, v in attributes.items() if v not in (None, [], {})},
                provenance={"source": SOURCE_NAME, "version": self.version, "ref": ref},
            )
