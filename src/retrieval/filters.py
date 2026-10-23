"""
Metadata filtering for product search candidates in Self Search.

Supports filtering by price range, brand, category, and minimum rating.
Provides both SQL WHERE clause generation (for database-level filtering)
and in-memory filtering for pre-fetched candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.pricing.fx import get_fx_rates, inr_sql, to_inr
from src.retrieval.constraints import (
    brand_matches,
    colour_matches,
    match_product_type,
    mentions_term,
    product_colours,
    product_materials,
    product_occasions,
)

# Product-dict key for a synthetic INR estimate, present only while filtering with
# ``use_price_estimates``. Retrievers remove it before returning products, so it
# never reaches prompts, evidence packs or answers.
ESTIMATE_KEY = "price_estimate_inr"
ESTIMATE_SQL = "(SELECT e.amount FROM price_estimates e WHERE e.product_id = {p}id AND e.currency = 'INR')"


def strip_estimates(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove the internal estimate key from product dicts (in place) and return them."""
    for product in products:
        product.pop(ESTIMATE_KEY, None)
    return products


@dataclass
class MetadataFilter:
    """Filter criteria for product candidate retrieval.

    All set fields are hard constraints: every retrieval mode returns only
    products that satisfy all of them (see ``matches``).
    """

    category: Optional[str] = None
    brand: Optional[str] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_rating: Optional[float] = None
    category_id: Optional[int] = None
    brand_id: Optional[int] = None
    # Phase 9 relevance fix (additive). Text constraints are checked against
    # product title/description words (src/retrieval/constraints.py).
    product_types: tuple[str, ...] = ()
    excluded_brands: tuple[str, ...] = ()
    excluded_terms: tuple[str, ...] = ()
    # Strict attributes (2026-10-22): met only when the listing states them (details or
    # title); a product whose colour/material is not stated never matches.
    colours: tuple[str, ...] = ()
    materials: tuple[str, ...] = ()
    # Occasions (2026-10-23), e.g. ("bridal",): met only when the listing states them.
    occasions: tuple[str, ...] = ()
    # Currency of min/max price. Budgets are in INR (2026-10-23): a bound is checked
    # against the product's normalised INR price (an INR source price, or a source price
    # converted with a documented rate, src/pricing/fx.py). Any other value matches nothing.
    price_currency: Optional[str] = None
    # Experimental: an INR bound may also be met by a synthetic INR estimate, but only
    # for a product with no source price. Set per turn by the router when estimates are
    # enabled (off by default); never serialised into active_filters.
    use_price_estimates: bool = False

    def is_empty(self) -> bool:
        """Return True if no filter constraints are set."""
        return all(
            v is None
            for v in (
                self.category,
                self.brand,
                self.min_price,
                self.max_price,
                self.min_rating,
                self.category_id,
                self.brand_id,
            )
        ) and not self.has_text_constraints()

    def has_text_constraints(self) -> bool:
        """True if constraints that can only be checked in Python (not SQL) are set."""
        return bool(self.product_types or self.excluded_brands or self.excluded_terms
                    or self.colours or self.materials or self.occasions or self.brand)

    def inr_price(self, product: dict[str, Any]) -> Optional[float]:
        """The product's normalised INR price for budget checks, or None.

        INR source prices as they are; other source prices only via a documented rate
        (whole rupees); an estimate only for a product with no source price, and only
        while estimates apply to this filter. Mirrors ``build_sql_conditions``.
        """
        price = product.get("price")
        if price is not None:
            if not product.get("currency") or float(price) <= 0:
                return None
            amount, _ = to_inr(float(price), product.get("currency"))
            return amount if amount is not None and amount > 0 else None
        if self._estimates_apply() and product.get(ESTIMATE_KEY) is not None:
            return float(product[ESTIMATE_KEY])
        return None

    def matches(self, product: dict[str, Any]) -> bool:
        """Check if a product dictionary satisfies all active filter criteria."""
        has_price_bound = self.min_price is not None or self.max_price is not None
        price = self.inr_price(product) if has_price_bound else None
        if has_price_bound and (self.price_currency or "INR").upper() != "INR":
            return False  # budgets are INR only; nothing else is ever compared

        if self.product_types and match_product_type(product, self.product_types) is None:
            return False

        if self.colours:
            shades = product_colours(product)[0]
            if not all(colour_matches(c, shades) for c in self.colours):
                return False

        if self.materials:
            found = product_materials(product)[0]
            if not all(m in found for m in self.materials):
                return False

        if self.occasions:
            stated = product_occasions(product)[0]
            if not all(o in stated for o in self.occasions):
                return False

        if self.excluded_brands and any(brand_matches(product, b) for b in self.excluded_brands):
            return False

        for term in self.excluded_terms:
            if mentions_term(product, term) is not None:
                return False

        if self.min_price is not None:
            if price is None or price < self.min_price:
                return False

        if self.max_price is not None:
            if price is None or price > self.max_price:
                return False

        if self.min_rating is not None:
            rating = product.get("avg_rating")
            if rating is None or rating < self.min_rating:
                return False

        if self.category_id is not None:
            cat_id = product.get("category_id")
            if cat_id != self.category_id:
                return False

        if self.brand_id is not None:
            b_id = product.get("brand_id")
            if b_id != self.brand_id:
                return False

        if self.category is not None:
            cat_name = (product.get("category_name") or product.get("category") or "").strip().lower()
            if self.category.strip().lower() not in cat_name and cat_name != self.category.strip().lower():
                return False

        if self.brand is not None and brand_matches(product, self.brand) is None:
            return False

        return True

    def _estimates_apply(self) -> bool:
        return (self.use_price_estimates and (self.price_currency or "INR").upper() == "INR"
                and (self.min_price is not None or self.max_price is not None))

    def estimate_column(self, table_prefix: str = "p") -> Optional[str]:
        """SQL expression selecting a product's INR estimate, when estimates apply to this filter."""
        return ESTIMATE_SQL.format(p=f"{table_prefix}." if table_prefix else "") if self._estimates_apply() else None

    def build_sql_conditions(self, table_prefix: str = "p") -> tuple[str, list[Any]]:
        """Build SQL WHERE clause fragments and parameter bindings.

        Args:
            table_prefix: SQL table alias for the products table.

        Returns:
            Tuple of (where_clause_sql, params_list)
        """
        clauses: list[str] = []
        params: list[Any] = []
        p = f"{table_prefix}." if table_prefix else ""

        if self.min_price is not None or self.max_price is not None:
            if (self.price_currency or "INR").upper() != "INR":
                clauses.append("0")  # budgets are INR only
            else:
                # Normalised INR source price (INR as is; documented conversions, whole rupees) ...
                expr, expr_params = inr_sql(f"{p}price", f"{p}currency", get_fx_rates())
                src, src_params = [f"{expr} > 0"], list(expr_params)
                est, est_params = [], []
                for op, bound in ((">=", self.min_price), ("<=", self.max_price)):
                    if bound is not None:
                        src.append(f"{expr} {op} ?")
                        src_params += expr_params + [bound]
                        est.append(f"e.amount {op} ?")
                        est_params.append(bound)
                if self._estimates_apply():
                    # ... or, only without a source price, a synthetic INR estimate.
                    clauses.append(
                        f"(({' AND '.join(src)}) OR ({p}price IS NULL AND EXISTS (SELECT 1 FROM price_estimates e "
                        f"WHERE e.product_id = {p}id AND e.currency = 'INR' AND {' AND '.join(est)})))"
                    )
                    params.extend(src_params + est_params)
                else:
                    clauses.append(f"({' AND '.join(src)})")
                    params.extend(src_params)

        if self.min_rating is not None:
            clauses.append(f"{p}avg_rating >= ?")
            params.append(self.min_rating)

        if self.category_id is not None:
            clauses.append(f"{p}category_id = ?")
            params.append(self.category_id)

        if self.brand_id is not None:
            clauses.append(f"{p}brand_id = ?")
            params.append(self.brand_id)

        if self.category is not None:
            clauses.append("LOWER(c.name) LIKE ?")
            params.append(f"%{self.category.strip().lower()}%")

        # Brand is checked in Python (``matches``): it may be stated by the brand field, the
        # store name or the start of the title, not only the brands table.

        where_sql = " AND ".join(clauses) if clauses else ""
        return where_sql, params
