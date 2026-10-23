"""
Typed numeric claim extraction and database verification for Self Search (Phase 6).

Extracts prices, ratings, review counts, and specifications from claim text,
cross-referencing them against SQLite database records.
Handles missing database attributes as UNVERIFIABLE rather than assuming falsehood.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Optional

from src.evidence.report import NumericCheckResult, NumericType
from src.pricing.budget import classify_price
from src.pricing.fx import format_inr


class NumericExtractor:
    """Regex-based extractor for typed numerical assertions in text.

    Distinguishes prices, ratings, review counts, and specifications without
    accidentally capturing unrelated general numbers (e.g. 'top 3 products'
    or 'version 2').
    """

    # INR price (2026-10-23): ₹1,499, ₹ 1,25,000, Rs. 799, Rs 799, INR 799, 799 rupees.
    # Indian grouping (1,25,000) and plain thousands grouping are both accepted.
    INR_PATTERN = re.compile(
        r"(?:(?:₹|\brs\.?|\binr)\s*([0-9]{1,3}(?:,[0-9]{2,3})+(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)"
        r"|\b([0-9]{1,3}(?:,[0-9]{2,3})+(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)\s*(?:rupees?|inr)\b)",
        re.IGNORECASE,
    )

    # Foreign-currency price: $12, $12.99, $1,299.00, 29.99 dollars, 30 USD
    PRICE_PATTERN = re.compile(
        r"(?:\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)"
        r"|\b([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)\s*(?:dollars|usd)\b"
        r"|\b(?:usd|\$)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)\b)",
        re.IGNORECASE,
    )

    # Star rating pattern: 4.5 stars, 4.4/5, 4.8 / 5.0, 4 out of 5
    RATING_PATTERN = re.compile(
        r"\b([1-5](?:\.[0-9]{1,2})?)\s*(?:stars?|/5(?:\.0)?|\s*out of 5)\b",
        re.IGNORECASE,
    )

    # Review count pattern: 8,900 reviews, 500 ratings, 1200 customer reviews
    COUNT_PATTERN = re.compile(
        r"\b([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\s*(?:customer\s+)?(?:reviews?|ratings?)\b",
        re.IGNORECASE,
    )

    # Spec pattern: 100W, 20000mAh, 40L, 32oz, 6ft, 6mm, 5 lbs, 10 hours
    SPEC_PATTERN = re.compile(
        r"\b([0-9]+(?:\.[0-9]+)?)\s*(W|watts?|mAh|mah|L|liters?|oz|ounces?|ft|feet|mm|cm|inches?|kg|lbs?|pounds?|g|grams?|hours?|hrs?|GB|TB|V|volts?)\b",
        re.IGNORECASE,
    )

    @classmethod
    def extract_inr_prices(cls, text: str) -> list[float]:
        prices: list[float] = []
        for match in cls.INR_PATTERN.finditer(text):
            val_str = match.group(1) or match.group(2)
            if val_str:
                try:
                    prices.append(float(val_str.replace(",", "")))
                except ValueError:
                    pass
        return prices

    @classmethod
    def extract_prices(cls, text: str) -> list[float]:
        """Foreign-currency (dollar) amounts. Self Search shows prices only in INR, so any
        such amount in a claim is a mismatch (``NumericVerifier``)."""
        prices: list[float] = []
        for match in cls.PRICE_PATTERN.finditer(text):
            val_str = match.group(1) or match.group(2) or match.group(3)
            if val_str:
                try:
                    prices.append(float(val_str.replace(",", "")))
                except ValueError:
                    pass
        return prices

    @classmethod
    def extract_ratings(cls, text: str) -> list[float]:
        matches = cls.RATING_PATTERN.findall(text)
        ratings: list[float] = []
        for m in matches:
            try:
                ratings.append(float(m))
            except ValueError:
                pass
        return ratings

    @classmethod
    def extract_counts(cls, text: str) -> list[int]:
        matches = cls.COUNT_PATTERN.findall(text)
        counts: list[int] = []
        for m in matches:
            try:
                counts.append(int(m.replace(",", "")))
            except ValueError:
                pass
        return counts

    @classmethod
    def extract_specs(cls, text: str) -> list[tuple[str, str]]:
        """Extract (value, unit) tuples, e.g. ('100', 'W') or ('20000', 'mAh')."""
        return cls.SPEC_PATTERN.findall(text)


class NumericVerifier:
    """Verifies extracted numeric claims against the SQLite product record.

    Tolerances:
    - price_tolerance (default $0.05):
      Accommodates floating-point conversion artifacts (e.g. $19.99 vs 19.990001)
      while flagging actual price discrepancies ($19.99 vs $29.99).
    - rating_tolerance (default 0.15 stars):
      Permits common language model rounding (e.g., rounding a 4.43 average rating
      to 4.4 or 4.5 stars) while catching hallucinations (e.g. 4.0 or 5.0 stars).
    - count_relative_tolerance (default 0.20 / 20%):
      Allows natural approximations for review counts (e.g. '8,000 reviews' when
      the catalog records 8,920 reviews) while identifying order-of-magnitude errors.

    Missing Database Values:
    - If a price, rating, review count, or specification is absent in the database,
      the verifier returns status='UNVERIFIABLE' with is_match=True.
      This adheres strictly to Requirement 4: missing DB values must produce
      UNVERIFIABLE rather than a false assertion or penalty.
    """

    def __init__(
        self,
        price_tolerance: float = 1.0,
        rating_tolerance: float = 0.15,
        count_relative_tolerance: float = 0.20,
    ) -> None:
        # Rupees: INR prices are whole rupees (documented conversions are rounded).
        self.price_tolerance = price_tolerance
        self.rating_tolerance = rating_tolerance
        self.count_relative_tolerance = count_relative_tolerance

    def verify_claims(
        self,
        conn: sqlite3.Connection,
        product_id: int,
        claim_text: str,
    ) -> list[NumericCheckResult]:
        """Verify all numeric assertions in claim_text against product_id in SQLite."""
        results: list[NumericCheckResult] = []

        row = conn.execute(
            "SELECT price, avg_rating, rating_count, attributes_json, title, currency FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()

        # Any foreign-currency amount is a mismatch: every price shown is INR.
        for p in NumericExtractor.extract_prices(claim_text):
            results.append(
                NumericCheckResult(
                    numeric_type=NumericType.PRICE,
                    claimed_value=p,
                    database_value=None,
                    is_match=False,
                    status="NUMERIC_MISMATCH",
                    details=f"Claim states a foreign-currency price ({p:,.2f}); Self Search shows prices only in INR.",
                )
            )

        if not row:
            # If product doesn't exist, all numeric claims for it are UNVERIFIABLE
            for p in NumericExtractor.extract_inr_prices(claim_text):
                results.append(
                    NumericCheckResult(
                        numeric_type=NumericType.PRICE,
                        claimed_value=p,
                        database_value=None,
                        is_match=True,
                        status="UNVERIFIABLE",
                        details=f"Product {product_id} not found in database; price cannot be verified.",
                    )
                )
            for r in NumericExtractor.extract_ratings(claim_text):
                results.append(
                    NumericCheckResult(
                        numeric_type=NumericType.RATING,
                        claimed_value=r,
                        database_value=None,
                        is_match=True,
                        status="UNVERIFIABLE",
                        details=f"Product {product_id} not found in database; rating cannot be verified.",
                    )
                )
            for c in NumericExtractor.extract_counts(claim_text):
                results.append(
                    NumericCheckResult(
                        numeric_type=NumericType.REVIEW_COUNT,
                        claimed_value=c,
                        database_value=None,
                        is_match=True,
                        status="UNVERIFIABLE",
                        details=f"Product {product_id} not found in database; review count cannot be verified.",
                    )
                )
            for val, unit in NumericExtractor.extract_specs(claim_text):
                results.append(
                    NumericCheckResult(
                        numeric_type=NumericType.SPECIFICATION,
                        claimed_value=f"{val}{unit}",
                        database_value=None,
                        is_match=True,
                        status="UNVERIFIABLE",
                        details=f"Product {product_id} not found in database; specification cannot be verified.",
                    )
                )
            return results

        db_price, db_rating, db_count, db_attrs_raw, db_title, db_currency = row
        db_attrs: dict[str, Any] = {}
        if db_attrs_raw:
            try:
                db_attrs = json.loads(db_attrs_raw)
            except Exception:
                db_attrs = {}

        # 1. Price checks, against the normalised INR source price (an INR source price, or a
        # documented conversion). Synthetic estimates are never ground truth for a claim.
        inr_price = classify_price(db_price, db_currency, None, estimates_enabled=False).amount
        prices = NumericExtractor.extract_inr_prices(claim_text)
        for claimed_price in prices:
            if inr_price is None:
                results.append(
                    NumericCheckResult(
                        numeric_type=NumericType.PRICE,
                        claimed_value=claimed_price,
                        database_value=None,
                        is_match=True,  # Do not flag as false assertion
                        status="UNVERIFIABLE",
                        details="Product has no INR price in the database (price unavailable).",
                    )
                )
            else:
                db_price_val = float(inr_price)
                diff = abs(claimed_price - db_price_val)
                is_match = diff <= self.price_tolerance
                status = "VERIFIED_MATCH" if is_match else "NUMERIC_MISMATCH"
                details = (
                    f"Claimed {format_inr(claimed_price)} matches the INR price {format_inr(db_price_val)}"
                    if is_match
                    else f"Claimed {format_inr(claimed_price)} contradicts the INR price {format_inr(db_price_val)}"
                )
                results.append(
                    NumericCheckResult(
                        numeric_type=NumericType.PRICE,
                        claimed_value=claimed_price,
                        database_value=db_price_val,
                        is_match=is_match,
                        status=status,
                        details=details,
                    )
                )

        # 2. Rating checks
        ratings = NumericExtractor.extract_ratings(claim_text)
        for claimed_rating in ratings:
            if db_rating is None:
                results.append(
                    NumericCheckResult(
                        numeric_type=NumericType.RATING,
                        claimed_value=claimed_rating,
                        database_value=None,
                        is_match=True,
                        status="UNVERIFIABLE",
                        details="Product has no average rating recorded in database.",
                    )
                )
            else:
                db_rating_val = float(db_rating)
                diff = abs(claimed_rating - db_rating_val)
                is_match = diff <= self.rating_tolerance
                status = "VERIFIED_MATCH" if is_match else "NUMERIC_MISMATCH"
                details = (
                    f"Claimed {claimed_rating:.1f} stars matches DB rating {db_rating_val:.1f}"
                    if is_match
                    else f"Claimed {claimed_rating:.1f} stars contradicts DB rating {db_rating_val:.1f}"
                )
                results.append(
                    NumericCheckResult(
                        numeric_type=NumericType.RATING,
                        claimed_value=claimed_rating,
                        database_value=db_rating_val,
                        is_match=is_match,
                        status=status,
                        details=details,
                    )
                )

        # 3. Review count checks
        counts = NumericExtractor.extract_counts(claim_text)
        for claimed_count in counts:
            if db_count is None:
                results.append(
                    NumericCheckResult(
                        numeric_type=NumericType.REVIEW_COUNT,
                        claimed_value=claimed_count,
                        database_value=None,
                        is_match=True,
                        status="UNVERIFIABLE",
                        details="Product has no review count recorded in database.",
                    )
                )
            else:
                db_count_val = int(db_count)
                # Allow relative tolerance or exact match
                diff = abs(claimed_count - db_count_val)
                rel_diff = diff / max(db_count_val, 1)
                is_match = diff == 0 or rel_diff <= self.count_relative_tolerance
                status = "VERIFIED_MATCH" if is_match else "NUMERIC_MISMATCH"
                details = (
                    f"Claimed count {claimed_count} aligns with DB count {db_count_val}"
                    if is_match
                    else f"Claimed count {claimed_count} contradicts DB count {db_count_val}"
                )
                results.append(
                    NumericCheckResult(
                        numeric_type=NumericType.REVIEW_COUNT,
                        claimed_value=claimed_count,
                        database_value=db_count_val,
                        is_match=is_match,
                        status=status,
                        details=details,
                    )
                )

        # 4. Specification checks
        specs = NumericExtractor.extract_specs(claim_text)
        searchable_text = f"{db_title or ''} {db_attrs_raw or ''}".lower()
        for val, unit in specs:
            unit_norm = unit.lower().rstrip("s")
            spec_matched = False
            # Check pattern in searchable text e.g. '100W', '100 W', '100 watt', '20000mah'
            pattern = rf"\b{re.escape(val)}\s*{re.escape(unit_norm[:2])}"
            if re.search(pattern, searchable_text):
                spec_matched = True
            elif f"{val}{unit.lower()}" in searchable_text or f"{val} {unit.lower()}" in searchable_text:
                spec_matched = True

            if spec_matched:
                results.append(
                    NumericCheckResult(
                        numeric_type=NumericType.SPECIFICATION,
                        claimed_value=f"{val}{unit}",
                        database_value=f"{val}{unit}",
                        is_match=True,
                        status="VERIFIED_MATCH",
                        details=f"Specification {val}{unit} found in product attributes/title.",
                    )
                )
            else:
                # Per Requirement 2 & 4: Return UNVERIFIABLE rather than assuming claim is false
                results.append(
                    NumericCheckResult(
                        numeric_type=NumericType.SPECIFICATION,
                        claimed_value=f"{val}{unit}",
                        database_value=None,
                        is_match=True,
                        status="UNVERIFIABLE",
                        details=f"Database attributes do not document specification '{val}{unit}'.",
                    )
                )

        return results
