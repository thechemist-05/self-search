"""
Resolve a parsed request into enforceable hard constraints (Phase 9 relevance fix).

Maps what the user asked for onto real database values and decides what can
be enforced honestly:

- Product types are checked against product title/description words.
- ``category`` is applied only when it names an existing catalogue category
  exactly; otherwise it becomes a product-type constraint (stored categories
  are too coarse: "shoes" is not "Sports_and_Outdoors").
- Budgets are in INR (2026-10-23), like every price Self Search shows. A bound is applied
  to each product's normalised INR price: an INR source price, or a source price converted
  with a documented rate (src/pricing/fx.py). A budget given without a currency is INR; a
  budget stated in another currency is **not converted** and triggers a clarification.
- Exclusions naming a catalogue brand exclude that brand; other exclusions
  exclude products whose title/description mention the term.
- Brands are detected deterministically (2026-10-22): a name in the brands table, or a
  store name used by at least ``MIN_STORE_PRODUCTS`` products, that appears as whole
  words in the message and is not an ordinary word (type, colour, material, fashion
  adjective). A product meets it only if its brand field, store or title start says so.
- Colours and materials are required exactly as stated; products whose listing does
  not state them are left out and counted (``unknown_attribute_products``).
- Experimental (off by default): when synthetic INR estimates are enabled and the
  database holds them, an INR budget is also met, for products with no source price
  only, by the labelled estimates. Unknown prices never satisfy a budget.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog

from src.agent.schemas import ConstraintCheck, ConstraintItem, ConstraintReport, ParsedQuery
from src.pricing.budget import classify_price
from src.pricing.fx import format_inr
from src.retrieval.constraints import (
    COLOURS,
    MATERIALS,
    PRODUCT_TYPES,
    STRICT_TYPES,
    brand_matches,
    match_product_type,
    mentions_term,
    product_colours,
    product_occasions,
    product_materials,
    product_type_for_term,
    type_phrases,
    words,
)
from src.retrieval.filters import ESTIMATE_KEY, MetadataFilter
from src.retrieval.keyword import STOPWORDS

logger = structlog.get_logger("shelfsense.agent.constraints")

TEXT_FIELDS = ["title", "description"]


def _norm(text: Optional[str]) -> str:
    return " ".join((text or "").replace("_", " ").split()).casefold()


MIN_STORE_PRODUCTS = 3
# Ordinary shopping words that are never read as a brand, even if a store has that name.
_FASHION_WORDS = frozenset("""
women womens woman men mens man kids kid girls girl boys boy baby toddler unisex adult ladies
summer winter spring autumn fall casual formal comfortable comfy waterproof lightweight cute sexy plus size
long short high low new fashion style classic vintage slim fit loose soft warm cool cotton party work
wedding beach running walking travel sport sports gym yoga outdoor indoor everyday office
""".split())


def _vocab_words() -> frozenset[str]:
    out = set(STOPWORDS) | _FASHION_WORDS
    for table in (PRODUCT_TYPES, COLOURS, MATERIALS):
        for key, phrases in table.items():
            for text in (key, *phrases):
                out.update(_brandless_tokens(text))
    return frozenset(out)


def _brandless_tokens(text: str) -> list[str]:
    import re

    return re.findall(r"[a-z0-9]+", (text or "").lower())


@dataclass
class Resolution:
    """Filter fields to apply, plus how the price constraint was handled."""

    fields: dict[str, Any] = field(default_factory=dict)
    price_requested: bool = False
    price_status: str = "applied"  # applied | not_applied_currency_mismatch
    stated_currency: Optional[str] = None
    currency_source: Optional[str] = None  # stated | catalog_default (no currency given: INR)
    requested_min: Optional[float] = None
    requested_max: Optional[float] = None
    catalog_currencies: list[str] = field(default_factory=list)
    uses_estimates: bool = False  # INR budget applied with synthetic estimates

    @property
    def price_blocked(self) -> bool:
        return self.price_requested and self.price_status != "applied"


class ConstraintResolver:
    """Database-aware constraint resolution and reporting."""

    def __init__(self, conn: sqlite3.Connection, estimates_enabled: Optional[bool] = None) -> None:
        self.conn = conn
        if estimates_enabled is None:
            from src.config.settings import get_settings

            estimates_enabled = get_settings().estimated_prices_enabled
        self.estimates_active = bool(estimates_enabled) and self._has_estimates()
        self._brand_index: Optional[dict[str, str]] = None

    def _has_estimates(self) -> bool:
        try:
            return self.conn.execute("SELECT 1 FROM price_estimates LIMIT 1").fetchone() is not None
        except Exception:  # no experimental table (production schema v2) or a mocked connection
            return False

    # -- catalogue facts ----------------------------------------------------

    def _names(self, sql: str) -> list[str]:
        try:
            return [r[0] for r in self.conn.execute(sql).fetchall() if r[0]]
        except Exception:  # e.g. a mocked connection in unit tests
            return []

    def brand_index(self) -> dict[str, str]:
        """Normalised brand name -> display name: the brands table plus frequent store names."""
        if self._brand_index is None:
            index: dict[str, str] = {}
            for name in self._names("SELECT name FROM brands"):
                index.setdefault(" ".join(_brandless_tokens(name)), name)
            counts: dict[str, int] = {}
            display: dict[str, str] = {}
            try:
                for (raw,) in self.conn.execute("SELECT attributes_json FROM products"):
                    try:
                        store = (json.loads(raw or "{}") or {}).get("store")
                    except (TypeError, ValueError):
                        continue
                    key = " ".join(_brandless_tokens(store or ""))
                    if key:
                        counts[key] = counts.get(key, 0) + 1
                        display.setdefault(key, str(store).strip())
            except Exception:  # e.g. a mocked connection in unit tests
                pass
            for key, n in counts.items():
                if n >= MIN_STORE_PRODUCTS:
                    index.setdefault(key, display[key])
            vocab = _vocab_words()
            self._brand_index = {k: v for k, v in index.items()
                                 if len(k) >= 3 and not all(t in vocab for t in k.split())}
        return self._brand_index

    def detect_brand(self, text: str) -> Optional[str]:
        """A catalogue brand named in ``text`` (longest whole-word match), else None."""
        index = self.brand_index()
        tokens = _brandless_tokens(text)
        for n in (4, 3, 2, 1):
            for i in range(len(tokens) - n + 1):
                key = " ".join(tokens[i:i + n])
                if key in index:
                    return index[key]
        return None

    def catalog_currencies(self) -> list[str]:
        """Currency every price is shown and filtered in: INR (source prices are normalised)."""
        return ["INR"]

    # -- resolution ---------------------------------------------------------

    def resolve(self, parsed: ParsedQuery) -> Resolution:
        """Turn a parsed request into MetadataFilter fields and a price decision."""
        fields: dict[str, Any] = parsed.to_metadata_filter_dict()
        ptypes = list(parsed.product_types)

        if parsed.category:
            categories = {_norm(c): c for c in self._names("SELECT name FROM categories")}
            exact = categories.get(_norm(parsed.category))
            if exact:
                fields["category"] = exact
            else:
                fields.pop("category", None)
                if not product_type_for_term(parsed.category):
                    literal = " ".join(words(parsed.category))
                    if literal and literal not in ptypes:
                        ptypes.append(literal)  # unknown type: matched literally in title/description

        brands = {_norm(b): b for b in self._names("SELECT name FROM brands")}
        index = self.brand_index()
        if parsed.brand and _norm(parsed.brand) in brands:
            fields["brand"] = brands[_norm(parsed.brand)]
        elif parsed.brand and " ".join(_brandless_tokens(parsed.brand)) in index:
            fields["brand"] = index[" ".join(_brandless_tokens(parsed.brand))]
        else:
            wanted_text = " ".join(t for t in [parsed.search_text or parsed.raw_query])
            for ex in parsed.exclusions:  # "not Nike" excludes Nike; it does not require it
                wanted_text = wanted_text.replace(ex, " ")
            found = self.detect_brand(wanted_text)
            if found:
                fields["brand"] = found

        excluded_brands, excluded_terms = [], []
        for ex in parsed.exclusions:
            key = " ".join(_brandless_tokens(ex))
            if _norm(ex) in brands:
                excluded_brands.append(brands[_norm(ex)])
            elif key in index:
                excluded_brands.append(index[key])
            else:
                excluded_terms.append(ex)

        fields["product_types"] = tuple(ptypes)
        fields["excluded_brands"] = tuple(excluded_brands)
        fields["excluded_terms"] = tuple(excluded_terms)
        fields["colours"] = tuple(parsed.colours)
        fields["materials"] = tuple(parsed.materials)
        fields["occasions"] = tuple(parsed.occasions)

        res = Resolution(fields=fields, catalog_currencies=self.catalog_currencies())
        res.price_requested = parsed.min_price is not None or parsed.max_price is not None
        res.requested_min, res.requested_max = parsed.min_price, parsed.max_price
        res.stated_currency = parsed.price_currency
        if res.price_requested:
            if parsed.price_currency and parsed.price_currency != "INR":
                res.price_status = "not_applied_currency_mismatch"  # never converted; asks for ₹
            else:
                fields["price_currency"] = "INR"
                res.currency_source = "stated" if parsed.price_currency else "catalog_default"
                res.uses_estimates = self.estimates_active
            if res.price_blocked:
                fields.pop("min_price", None)
                fields.pop("max_price", None)
                logger.info("price_constraint_not_applied", status=res.price_status,
                            stated=parsed.price_currency)
        return res

    # -- counting and reporting ---------------------------------------------

    def matching_products(self, filters: MetadataFilter) -> list[dict[str, Any]]:
        """Every catalogue product satisfying all applied constraints."""
        where, params = filters.build_sql_conditions(table_prefix="p")
        estimate_col = filters.estimate_column("p")
        try:
            rows = self.conn.execute(
                "SELECT p.id, p.title, p.description, p.price, p.currency, p.avg_rating, "
                "c.name, b.name, p.category_id, p.brand_id, p.attributes_json"
                + (f", {estimate_col}" if estimate_col else "") + " FROM products p "
                "LEFT JOIN categories c ON c.id = p.category_id LEFT JOIN brands b ON b.id = p.brand_id"
                + (f" WHERE {where}" if where else ""),
                params,
            ).fetchall()
        except Exception:
            return []
        products = [
            {"id": r[0], "title": r[1], "description": r[2], "price": r[3], "currency": r[4],
             "avg_rating": r[5], "category_name": r[6], "brand_name": r[7], "category_id": r[8],
             "brand_id": r[9], "attributes_json": r[10], **({ESTIMATE_KEY: r[11]} if estimate_col else {})}
            for r in rows
        ]
        return [p for p in products if filters.matches(p)]

    def report(self, filters: MetadataFilter, res: Resolution) -> ConstraintReport:
        """Describe the constraints in force for this turn (after session merging)."""
        items: list[ConstraintItem] = []
        if filters.product_types:
            strict = all(t in STRICT_TYPES for t in filters.product_types)
            if strict:
                label = (f"Product type: {', '.join(filters.product_types)} (stated in the title; other "
                         "types and accessories that only mention it are excluded)")
            else:
                terms = sorted({t for ptype in filters.product_types for t in type_phrases(ptype)})
                label = (f"Product type: {', '.join(filters.product_types)} (title or description mentions: "
                         f"{', '.join(terms)})")
            items.append(ConstraintItem(
                kind="product_type", value=list(filters.product_types),
                checked_fields=["title"] if strict else TEXT_FIELDS, label=label,
            ))
        for c in filters.colours:
            items.append(ConstraintItem(kind="colour", value=c, checked_fields=["details.Color", "title"],
                                        label=f"Colour: {c} (stated by the listing)"))
        for m in filters.materials:
            items.append(ConstraintItem(kind="material", value=m, checked_fields=["details.Material", "title"],
                                        label=f"Material: {m} (stated by the listing)"))
        for o in filters.occasions:
            items.append(ConstraintItem(kind="occasion", value=o,
                                        checked_fields=["details.Occasion", "title", "description"],
                                        label=f"Occasion: {o} (stated by the listing)"))
        if filters.category:
            items.append(ConstraintItem(kind="category", value=filters.category, checked_fields=["category"],
                                        label=f"Category: {filters.category.replace('_', ' ')}"))
        if filters.brand:
            items.append(ConstraintItem(kind="brand", value=filters.brand, checked_fields=["brand"],
                                        label=f"Brand: {filters.brand}"))

        if filters.min_price is not None or filters.max_price is not None:
            cur = filters.price_currency or "INR"
            source = res.currency_source if res.price_requested else "earlier_turn"
            note = " (no currency given; all prices are in ₹)" if source == "catalog_default" else ""
            note += " (prices in ₹, including source prices converted at a documented exchange rate"
            estimated = filters.use_price_estimates and cur.upper() == "INR"
            if estimated:
                note += ("; products without a source price are matched on synthetic estimates: "
                         "illustrative, not retailer prices")
            note += "; unknown prices never match)"
            items.append(ConstraintItem(
                kind="price", checked_fields=["price_inr"] + (["price_estimate"] if estimated else []),
                value={"min": filters.min_price, "max": filters.max_price, "currency": cur,
                       "currency_source": source, "uses_synthetic_estimates": estimated},
                label=_price_label(filters.min_price, filters.max_price, cur) + note,
            ))
        elif res.price_blocked:
            reason = (f"Your budget is in {res.stated_currency}, but Self Search shows every price in Indian "
                      "rupees (₹), so budgets are in ₹ too. A budget in another currency is not converted.")
            items.append(ConstraintItem(
                kind="price", status="not_applied", reason=reason, checked_fields=["price", "currency"],
                value={"min": res.requested_min, "max": res.requested_max, "currency": res.stated_currency},
                label="Budget not in rupees",  # a foreign amount is never shown, not even the user's budget
            ))

        if filters.min_rating is not None:
            items.append(ConstraintItem(kind="min_rating", value=filters.min_rating, checked_fields=["avg_rating"],
                                        label=f"Catalogue rating ≥ {filters.min_rating}"))
        for b in filters.excluded_brands:
            items.append(ConstraintItem(kind="excluded_brand", value=b, checked_fields=["brand"],
                                        label=f"Not brand {b}"))
        for t in filters.excluded_terms:
            items.append(ConstraintItem(kind="excluded_term", value=t, checked_fields=TEXT_FIELDS,
                                        label=f"Excludes products mentioning “{t}”"))

        return ConstraintReport(
            items=items,
            matching_products=len(self.matching_products(filters)),
            catalog_currencies=res.catalog_currencies,
            unknown_attribute_products=self.unknown_attribute_products(filters),
        )

    def unknown_attribute_products(self, filters: MetadataFilter) -> Optional[int]:
        """Products meeting every other constraint whose listing doesn't state a required colour/material.

        They are left out of strict results (an unstated colour is never assumed); the count lets
        the answer say so instead of implying the catalogue has nothing of that type.
        """
        if not (filters.colours or filters.materials or filters.occasions):
            return None
        from dataclasses import replace

        rest = self.matching_products(replace(filters, colours=(), materials=(), occasions=()))
        return sum(1 for p in rest
                   if (filters.colours and not product_colours(p)[0])
                   or (filters.materials and not product_materials(p)[0])
                   or (filters.occasions and not product_occasions(p)[0]))

    def clarification(self, res: Resolution, report: ConstraintReport) -> str:
        """Message for a budget that cannot be applied, without converting anything."""
        n = report.matching_products or 0
        others = (
            f"{n} product{'s' if n != 1 else ''} in the catalogue {'match' if n != 1 else 'matches'} "
            "the rest of your request."
            if n else "No product in the catalogue matches the rest of your request either."
        )
        return (
            f"Self Search shows every price in Indian rupees (₹), so I can't apply a budget in "
            f"{res.stated_currency or 'another currency'}, and I won't convert it. {others} Please tell me "
            "your budget in ₹ (for example “… under ₹3,000”)."
        )


def _price_label(lo: Optional[float], hi: Optional[float], cur: Optional[str]) -> str:
    """"Max price: ₹3,000" for INR; another currency is named, never given a ₹ sign."""
    def amount(v: float) -> str:
        return format_inr(v) if (cur or "INR").upper() == "INR" else f"{cur} {v:,.2f}"
    if lo is not None and hi is not None:
        return f"Price: {amount(lo)} – {amount(hi)}"
    if hi is not None:
        return f"Max price: {amount(hi)}"
    return f"Min price: {amount(lo)}"


def constraint_checks(product: dict[str, Any], filters: MetadataFilter) -> list[ConstraintCheck]:
    """Database evidence that ``product`` satisfies each applied constraint in ``filters``."""
    checks: list[ConstraintCheck] = []
    if filters.product_types:
        hit = match_product_type(product, filters.product_types)
        if hit:
            ptype, fld, phrase = hit
            checks.append(ConstraintCheck(kind="product_type",
                                          detail=f"{ptype}: {fld} mentions “{phrase}”"))
    if filters.category:
        checks.append(ConstraintCheck(kind="category", detail=f"Category is {(product.get('category_name') or '').replace('_', ' ')}"))
    if filters.brand:
        where = brand_matches(product, filters.brand)
        if where:
            checks.append(ConstraintCheck(kind="brand", detail=f"Brand {filters.brand} (from the listing's {where})"))
    if filters.colours:
        shades, source = product_colours(product)
        if shades:
            checks.append(ConstraintCheck(kind="colour", detail=f"Colour: {', '.join(shades)} (stated in the {source})"))
    if filters.materials:
        found, source = product_materials(product)
        if found:
            checks.append(ConstraintCheck(kind="material", detail=f"Material: {', '.join(found)} (stated in the {source})"))
    if filters.occasions:
        stated, source = product_occasions(product)
        if stated:
            checks.append(ConstraintCheck(kind="occasion", detail=f"Occasion: {', '.join(stated)} (stated in the {source})"))
    if filters.min_price is not None or filters.max_price is not None:
        view = classify_price(product.get("price"), product.get("currency"), None, estimates_enabled=False)
        estimate = product.get(ESTIMATE_KEY) if product.get("price") is None and filters.use_price_estimates else None
        if view.amount is not None:
            how = (f" (converted from the source price at {view.fx.describe()})" if view.status == "converted"
                   else " (source price)")
            amount, prefix, suffix = view.amount, "", how
        elif estimate is not None:
            amount, prefix, suffix = float(estimate), "Synthetic estimate ", " (illustrative estimate, not a retailer price)"
        else:
            amount = None
        if amount is not None:
            for op, bound in (("≤", filters.max_price), ("≥", filters.min_price)):
                if bound is not None:
                    checks.append(ConstraintCheck(kind="price", detail=(
                        f"{prefix}{format_inr(amount)} {op} {format_inr(bound)}{suffix}")))
    if filters.min_rating is not None and product.get("avg_rating") is not None:
        checks.append(ConstraintCheck(kind="min_rating",
                                      detail=f"Catalogue rating {product['avg_rating']:.1f} ≥ {filters.min_rating}"))
    for b in filters.excluded_brands:
        checks.append(ConstraintCheck(kind="excluded_brand", detail=f"Brand is not {b}"))
    for t in filters.excluded_terms:
        if mentions_term(product, t) is None:
            checks.append(ConstraintCheck(kind="excluded_term", detail=f"Title and description don't mention “{t}”"))
    return checks
