"""Unit tests for data ingestion from fixture data."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.data.ingestion import ingest_from_fixture
from src.data.source_registry import get_source_by_name, register_source, list_sources
from src.db.connection import get_connection
from src.db.migrations import run_migrations


@pytest.fixture
def db_conn(tmp_path: Path):
    """Create a fresh database connection with schema."""
    conn = get_connection(tmp_path / "test.db")
    run_migrations(conn)
    yield conn
    conn.close()


class TestIngestFromFixture:
    """Tests for ingesting fixture data."""

    def test_ingests_all_products(self, db_conn, fixture_products, fixture_reviews) -> None:
        """All 20 fixture products are inserted."""
        stats = ingest_from_fixture(db_conn, fixture_products, fixture_reviews)
        assert stats["products_inserted"] == 20
        assert stats["products_skipped"] == 0

    def test_ingests_all_reviews(self, db_conn, fixture_products, fixture_reviews) -> None:
        """All 100 fixture reviews are inserted."""
        stats = ingest_from_fixture(db_conn, fixture_products, fixture_reviews)
        assert stats["reviews_inserted"] == 100
        assert stats["reviews_skipped"] == 0

    def test_correct_product_count_in_db(self, db_conn, fixture_products, fixture_reviews) -> None:
        """Database contains the correct number of products after ingestion."""
        ingest_from_fixture(db_conn, fixture_products, fixture_reviews)
        row = db_conn.execute("SELECT COUNT(*) FROM products").fetchone()
        assert row[0] == 20

    def test_correct_review_count_in_db(self, db_conn, fixture_products, fixture_reviews) -> None:
        """Database contains the correct number of reviews after ingestion."""
        ingest_from_fixture(db_conn, fixture_products, fixture_reviews)
        row = db_conn.execute("SELECT COUNT(*) FROM reviews").fetchone()
        assert row[0] == 100

    def test_categories_created(self, db_conn, fixture_products, fixture_reviews) -> None:
        """Categories from fixture data are created in the categories table."""
        ingest_from_fixture(db_conn, fixture_products, fixture_reviews)
        rows = db_conn.execute("SELECT name FROM categories").fetchall()
        names = {row[0] for row in rows}
        assert "Electronics" in names
        assert "Sports_and_Outdoors" in names

    def test_brands_created(self, db_conn, fixture_products, fixture_reviews) -> None:
        """Brands from fixture data are created in the brands table."""
        ingest_from_fixture(db_conn, fixture_products, fixture_reviews)
        row = db_conn.execute("SELECT COUNT(*) FROM brands").fetchone()
        assert row[0] == 20  # 20 unique brands in fixture

    def test_idempotent_rerun(self, db_conn, fixture_products, fixture_reviews) -> None:
        """Running ingestion twice doesn't duplicate data."""
        stats1 = ingest_from_fixture(db_conn, fixture_products, fixture_reviews)
        stats2 = ingest_from_fixture(db_conn, fixture_products, fixture_reviews)

        assert stats1["products_inserted"] == 20
        assert stats2["products_inserted"] == 0
        assert stats2["products_skipped"] == 20

        row = db_conn.execute("SELECT COUNT(*) FROM products").fetchone()
        assert row[0] == 20

    def test_reviews_linked_to_products(self, db_conn, fixture_products, fixture_reviews) -> None:
        """Every review is linked to a valid product."""
        ingest_from_fixture(db_conn, fixture_products, fixture_reviews)
        orphans = db_conn.execute(
            "SELECT COUNT(*) FROM reviews r "
            "LEFT JOIN products p ON r.product_id = p.id "
            "WHERE p.id IS NULL"
        ).fetchone()
        assert orphans[0] == 0

    def test_product_fields_preserved(self, db_conn, fixture_products, fixture_reviews) -> None:
        """Product fields are correctly stored."""
        ingest_from_fixture(db_conn, fixture_products, fixture_reviews)
        row = db_conn.execute(
            "SELECT title, price, avg_rating FROM products WHERE external_id = 'P001'"
        ).fetchone()
        assert row is not None
        assert row[0] == "UltraLight Hiking Backpack 40L"
        assert row[1] == 49.99
        assert row[2] == 4.3

    def test_review_fields_preserved(self, db_conn, fixture_products, fixture_reviews) -> None:
        """Review fields are correctly stored."""
        ingest_from_fixture(db_conn, fixture_products, fixture_reviews)
        row = db_conn.execute(
            "SELECT r.rating, r.title, r.verified_flag "
            "FROM reviews r JOIN products p ON r.product_id = p.id "
            "WHERE p.external_id = 'P001' AND r.title = 'Perfect hiking companion'"
        ).fetchone()
        assert row is not None
        assert row[0] == 5.0
        assert row[2] == 1  # verified

    def test_fts_index_populated(self, db_conn, fixture_products, fixture_reviews) -> None:
        """FTS5 index is populated via triggers after insertion."""
        ingest_from_fixture(db_conn, fixture_products, fixture_reviews)
        # Search for a product by keyword
        rows = db_conn.execute(
            "SELECT rowid FROM products_fts WHERE products_fts MATCH 'backpack'"
        ).fetchall()
        assert len(rows) >= 1


class TestSourceRegistry:
    """Tests for the data source registry."""

    def test_register_new_source(self, db_conn) -> None:
        """register_source creates a new entry."""
        source_id = register_source(db_conn, name="test-source", url="http://example.com")
        assert source_id > 0

    def test_register_returns_existing_id(self, db_conn) -> None:
        """register_source returns the same ID for an existing source."""
        id1 = register_source(db_conn, name="test-source")
        id2 = register_source(db_conn, name="test-source")
        assert id1 == id2

    def test_get_source_by_name(self, db_conn) -> None:
        """get_source_by_name returns the correct source."""
        register_source(db_conn, name="my-source", url="http://example.com", license="MIT")
        source = get_source_by_name(db_conn, "my-source")
        assert source is not None
        assert source.name == "my-source"
        assert source.url == "http://example.com"
        assert source.license == "MIT"

    def test_get_nonexistent_source(self, db_conn) -> None:
        """get_source_by_name returns None for a non-existent source."""
        source = get_source_by_name(db_conn, "nonexistent")
        assert source is None

    def test_list_sources(self, db_conn) -> None:
        """list_sources returns all registered sources."""
        register_source(db_conn, name="source-1")
        register_source(db_conn, name="source-2")
        sources = list_sources(db_conn)
        assert len(sources) == 2
