"""Unit tests for data validation rules."""

from __future__ import annotations

import pytest

from src.data.validation import (
    ValidationResult,
    clean_text,
    parse_price,
    validate_product,
    validate_review,
)


class TestParsePrice:
    """Tests for the parse_price function."""

    def test_float_input(self) -> None:
        assert parse_price(29.99) == 29.99

    def test_int_input(self) -> None:
        assert parse_price(50) == 50.0

    def test_string_dollar_sign(self) -> None:
        assert parse_price("$29.99") == 29.99

    def test_string_plain(self) -> None:
        assert parse_price("29.99") == 29.99

    def test_string_with_commas(self) -> None:
        assert parse_price("$1,299.99") == 1299.99

    def test_empty_string(self) -> None:
        assert parse_price("") is None

    def test_none(self) -> None:
        assert parse_price(None) is None

    def test_non_numeric_string(self) -> None:
        assert parse_price("free") is None

    def test_zero(self) -> None:
        assert parse_price(0) == 0.0

    def test_negative_returns_none(self) -> None:
        assert parse_price(-10.0) is None

    def test_currency_symbol_euro(self) -> None:
        assert parse_price("€49.99") == 49.99

    def test_whitespace(self) -> None:
        assert parse_price("  $12.50  ") == 12.50


class TestCleanText:
    """Tests for the clean_text function."""

    def test_strips_html(self) -> None:
        assert clean_text("<p>Hello <b>world</b></p>") == "Hello world"

    def test_normalises_whitespace(self) -> None:
        assert clean_text("hello   world\n\tfoo") == "hello world foo"

    def test_none_returns_empty(self) -> None:
        assert clean_text(None) == ""

    def test_list_input(self) -> None:
        assert clean_text(["hello", "world"]) == "hello world"

    def test_plain_text_unchanged(self) -> None:
        assert clean_text("normal text") == "normal text"


class TestValidateProduct:
    """Tests for product validation."""

    def test_valid_product(self) -> None:
        result = validate_product({
            "title": "Test Product",
            "external_id": "P001",
            "price": 29.99,
            "avg_rating": 4.5,
            "description": "A test product description",
        })
        assert result.is_valid is True
        assert len(result.errors) == 0

    def test_missing_title_rejected(self) -> None:
        result = validate_product({"external_id": "P001", "price": 10.0})
        assert result.is_valid is False
        assert any("title" in e for e in result.errors)

    def test_empty_title_rejected(self) -> None:
        result = validate_product({"title": "  ", "external_id": "P001"})
        assert result.is_valid is False

    def test_missing_external_id_rejected(self) -> None:
        result = validate_product({"title": "Test"})
        assert result.is_valid is False
        assert any("external_id" in e for e in result.errors)

    def test_negative_price_rejected(self) -> None:
        result = validate_product({
            "title": "Test", "external_id": "P001", "price": -5.0,
        })
        assert result.is_valid is False
        assert any("negative_price" in e for e in result.errors)

    def test_rating_out_of_range_rejected(self) -> None:
        result = validate_product({
            "title": "Test", "external_id": "P001", "avg_rating": 6.0,
        })
        assert result.is_valid is False
        assert any("rating_out_of_range" in e for e in result.errors)

    def test_missing_price_is_warning(self) -> None:
        result = validate_product({"title": "Test", "external_id": "P001"})
        assert result.is_valid is True
        assert any("missing_price" in w for w in result.warnings)

    def test_missing_description_is_warning(self) -> None:
        result = validate_product({
            "title": "Test", "external_id": "P001", "price": 10.0,
        })
        assert result.is_valid is True
        assert any("missing_description" in w for w in result.warnings)


class TestValidateReview:
    """Tests for review validation."""

    def test_valid_review(self) -> None:
        result = validate_review({
            "text": "Great product!",
            "product_id": 1,
            "rating": 5.0,
        })
        assert result.is_valid is True

    def test_missing_text_rejected(self) -> None:
        result = validate_review({"product_id": 1, "rating": 5.0})
        assert result.is_valid is False
        assert any("review_text" in e for e in result.errors)

    def test_empty_text_rejected(self) -> None:
        result = validate_review({"text": "  ", "product_id": 1})
        assert result.is_valid is False

    def test_missing_product_link_rejected(self) -> None:
        result = validate_review({"text": "Great!", "rating": 5.0})
        assert result.is_valid is False
        assert any("product_link" in e for e in result.errors)

    def test_review_rating_out_of_range(self) -> None:
        result = validate_review({
            "text": "Test", "product_id": 1, "rating": 0.0,
        })
        assert result.is_valid is False

    def test_parent_asin_counts_as_product_link(self) -> None:
        result = validate_review({
            "text": "Good!", "parent_asin": "B001", "rating": 4.0,
        })
        assert result.is_valid is True
