"""Unit tests for deduplication utilities."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.data.deduplication import (
    compute_review_hash,
    count_products,
    count_reviews,
    product_exists,
    review_exists,
)
from src.db.connection import get_connection
from src.db.migrations import run_migrations


@pytest.fixture
def db_conn(tmp_path: Path):
    """Create a fresh database connection with schema."""
    conn = get_connection(tmp_path / "test.db")
    run_migrations(conn)
    conn.execute("INSERT INTO data_sources (name) VALUES ('test')")
    conn.commit()
    yield conn
    conn.close()


class TestProductExists:
    """Tests for product_exists."""

    def test_returns_none_when_not_exists(self, db_conn) -> None:
        assert product_exists(db_conn, 1, "NONEXISTENT") is None

    def test_returns_id_when_exists(self, db_conn) -> None:
        db_conn.execute(
            "INSERT INTO products (source_id, external_id, title) VALUES (1, 'X1', 'Test')"
        )
        db_conn.commit()
        result = product_exists(db_conn, 1, "X1")
        assert result is not None
        assert isinstance(result, int)

    def test_different_source_not_found(self, db_conn) -> None:
        db_conn.execute(
            "INSERT INTO products (source_id, external_id, title) VALUES (1, 'X1', 'Test')"
        )
        db_conn.commit()
        assert product_exists(db_conn, 999, "X1") is None


class TestReviewExists:
    """Tests for review_exists."""

    def test_returns_none_when_not_exists(self, db_conn) -> None:
        assert review_exists(db_conn, 1, "NONEXISTENT") is None

    def test_returns_id_when_exists(self, db_conn) -> None:
        db_conn.execute(
            "INSERT INTO products (source_id, external_id, title) VALUES (1, 'X1', 'Test')"
        )
        db_conn.execute(
            "INSERT INTO reviews (product_id, source_id, external_id, text) "
            "VALUES (1, 1, 'R1', 'Great!')"
        )
        db_conn.commit()
        result = review_exists(db_conn, 1, "R1")
        assert result is not None


class TestComputeReviewHash:
    """Tests for compute_review_hash."""

    def test_same_input_same_hash(self) -> None:
        h1 = compute_review_hash("U1", "P1", "Great product!")
        h2 = compute_review_hash("U1", "P1", "Great product!")
        assert h1 == h2

    def test_different_text_different_hash(self) -> None:
        h1 = compute_review_hash("U1", "P1", "Great!")
        h2 = compute_review_hash("U1", "P1", "Terrible!")
        assert h1 != h2

    def test_different_user_different_hash(self) -> None:
        h1 = compute_review_hash("U1", "P1", "Great!")
        h2 = compute_review_hash("U2", "P1", "Great!")
        assert h1 != h2

    def test_returns_hex_string(self) -> None:
        h = compute_review_hash("U1", "P1", "Test")
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex digest


class TestCounts:
    """Tests for count_products and count_reviews."""

    def test_count_products_empty(self, db_conn) -> None:
        assert count_products(db_conn) == 0

    def test_count_products_with_data(self, db_conn) -> None:
        db_conn.execute(
            "INSERT INTO products (source_id, external_id, title) VALUES (1, 'X1', 'A')"
        )
        db_conn.execute(
            "INSERT INTO products (source_id, external_id, title) VALUES (1, 'X2', 'B')"
        )
        db_conn.commit()
        assert count_products(db_conn) == 2
        assert count_products(db_conn, source_id=1) == 2

    def test_count_reviews_empty(self, db_conn) -> None:
        assert count_reviews(db_conn) == 0
