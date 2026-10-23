"""
Data validation rules for Self Search ingestion.

Validates individual product and review records before insertion,
ensuring required fields are present and values are within acceptable ranges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog

logger = structlog.get_logger("shelfsense.data.validation")


@dataclass
class ValidationResult:
    """Result of validating a single record."""

    is_valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        """Add an error and mark the result as invalid."""
        self.errors.append(msg)
        self.is_valid = False

    def add_warning(self, msg: str) -> None:
        """Add a warning (record is still valid)."""
        self.warnings.append(msg)


def validate_product(record: dict[str, Any]) -> ValidationResult:
    """Validate a product record before insertion.

    Required: title, external_id.
    Recommended: description, price.

    Args:
        record: Dictionary with product fields.

    Returns:
        ValidationResult with is_valid flag and any errors/warnings.
    """
    result = ValidationResult()

    # Required fields
    if not record.get("title") or not str(record["title"]).strip():
        result.add_error("missing_or_empty_title")

    if not record.get("external_id") or not str(record["external_id"]).strip():
        result.add_error("missing_or_empty_external_id")

    # Price validation
    price = record.get("price")
    if price is not None:
        # Directly flag negative numeric values before parsing
        if isinstance(price, (int, float)) and price < 0:
            result.add_error(f"negative_price: {price}")
        else:
            parsed = parse_price(price)
            if parsed is not None and parsed < 0:
                result.add_error(f"negative_price: {price}")
    else:
        result.add_warning("missing_price")

    # Rating validation
    avg_rating = record.get("avg_rating")
    if avg_rating is not None:
        try:
            rating_val = float(avg_rating)
            if rating_val < 0 or rating_val > 5:
                result.add_error(f"rating_out_of_range: {avg_rating}")
        except (ValueError, TypeError):
            result.add_error(f"invalid_rating_type: {avg_rating}")

    # Description check
    if not record.get("description") or not str(record["description"]).strip():
        result.add_warning("missing_description")

    return result


def validate_review(record: dict[str, Any]) -> ValidationResult:
    """Validate a review record before insertion.

    Required: text (non-empty), product_id or product_external_id.

    Args:
        record: Dictionary with review fields.

    Returns:
        ValidationResult with is_valid flag and any errors/warnings.
    """
    result = ValidationResult()

    # Must have review text
    text = record.get("text")
    if not text or not str(text).strip():
        result.add_error("missing_or_empty_review_text")

    # Must link to a product
    if not record.get("product_id") and not record.get("parent_asin"):
        result.add_error("missing_product_link")

    # Rating validation
    rating = record.get("rating")
    if rating is not None:
        try:
            rating_val = float(rating)
            if rating_val < 1.0 or rating_val > 5.0:
                result.add_error(f"review_rating_out_of_range: {rating}")
        except (ValueError, TypeError):
            result.add_error(f"invalid_review_rating_type: {rating}")
    else:
        result.add_warning("missing_review_rating")

    return result


def parse_price(price_value: Any) -> Optional[float]:
    """Parse a price value from various formats into a float.

    Handles: "$29.99", "29.99", 29.99, "$1,299.99", "", None.

    Args:
        price_value: Raw price value from the dataset.

    Returns:
        Float price or None if unparseable.
    """
    if price_value is None:
        return None

    if isinstance(price_value, (int, float)):
        return float(price_value) if price_value >= 0 else None

    price_str = str(price_value).strip()
    if not price_str:
        return None

    # Remove currency symbols and commas
    cleaned = re.sub(r"[^\d.]", "", price_str)

    if not cleaned:
        return None

    try:
        return float(cleaned)
    except ValueError:
        logger.debug("unparseable_price", raw=price_value)
        return None


def clean_text(text: Any) -> str:
    """Clean text by stripping HTML tags and normalising whitespace.

    Args:
        text: Raw text (possibly containing HTML).

    Returns:
        Cleaned text string.
    """
    if text is None:
        return ""

    if isinstance(text, list):
        text = " ".join(str(t) for t in text)

    text = str(text)

    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", text)

    # Normalise whitespace
    text = re.sub(r"\s+", " ", text).strip()

    return text
