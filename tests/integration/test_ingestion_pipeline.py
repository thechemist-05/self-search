"""Integration test: full ingestion pipeline on fixture data."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.data.ingestion import ingest_from_fixture
from src.data.statistics import generate_statistics, format_report
from src.db.connection import get_connection
from src.db.migrations import run_migrations, verify_schema


class TestIngestionPipeline:
    """End-to-end tests for the ingestion pipeline."""

    @pytest.fixture
    def populated_db(self, tmp_path: Path, fixture_products, fixture_reviews):
        """Create a database with fixture data ingested."""
        db_path = tmp_path / "integration.db"
        conn = get_connection(db_path)
        run_migrations(conn)
        ingest_from_fixture(conn, fixture_products, fixture_reviews)
        yield conn
        conn.close()

    def test_pipeline_creates_valid_schema(self, populated_db) -> None:
        """After pipeline, schema is complete."""
        missing = verify_schema(populated_db)
        assert missing == []

    def test_pipeline_correct_product_count(self, populated_db) -> None:
        """Pipeline inserts exactly 20 products."""
        row = populated_db.execute("SELECT COUNT(*) FROM products").fetchone()
        assert row[0] == 20

    def test_pipeline_correct_review_count(self, populated_db) -> None:
        """Pipeline inserts exactly 100 reviews."""
        row = populated_db.execute("SELECT COUNT(*) FROM reviews").fetchone()
        assert row[0] == 100

    def test_pipeline_reviews_per_product(self, populated_db) -> None:
        """Each product has exactly 5 reviews."""
        rows = populated_db.execute(
            "SELECT p.external_id, COUNT(r.id) as review_count "
            "FROM products p JOIN reviews r ON r.product_id = p.id "
            "GROUP BY p.id ORDER BY review_count"
        ).fetchall()
        for row in rows:
            assert row[1] == 5, f"Product {row[0]} has {row[1]} reviews, expected 5"

    def test_pipeline_category_distribution(self, populated_db) -> None:
        """Products are split across 2 categories."""
        rows = populated_db.execute(
            "SELECT c.name, COUNT(p.id) FROM categories c "
            "JOIN products p ON p.category_id = c.id "
            "GROUP BY c.id"
        ).fetchall()
        cat_counts = {row[0]: row[1] for row in rows}
        assert cat_counts.get("Electronics", 0) == 10
        assert cat_counts.get("Sports_and_Outdoors", 0) == 10

    def test_pipeline_fts_search_works(self, populated_db) -> None:
        """FTS5 search finds products by keyword."""
        results = populated_db.execute(
            "SELECT rowid FROM products_fts WHERE products_fts MATCH 'wireless bluetooth'"
        ).fetchall()
        assert len(results) >= 1

    def test_pipeline_fts_review_search_works(self, populated_db) -> None:
        """FTS5 review search finds reviews by keyword."""
        results = populated_db.execute(
            "SELECT rowid FROM reviews_fts WHERE reviews_fts MATCH 'waterproof'"
        ).fetchall()
        assert len(results) >= 1

    def test_statistics_report_generated(self, populated_db) -> None:
        """Statistics can be generated from the populated database."""
        stats = generate_statistics(populated_db)
        assert stats.total_products == 20
        assert stats.total_reviews == 100
        assert stats.total_categories == 2
        assert stats.avg_reviews_per_product == 5.0

    def test_statistics_report_formatted(self, populated_db) -> None:
        """Statistics report formats as a non-empty markdown string."""
        stats = generate_statistics(populated_db)
        report = format_report(stats)
        assert isinstance(report, str)
        assert "# Self Search Data Statistics Report" in report
        assert "20" in report  # product count
        assert "100" in report  # review count

    def test_statistics_price_coverage(self, populated_db) -> None:
        """Statistics correctly count products with prices."""
        stats = generate_statistics(populated_db)
        assert stats.products_with_price == 20  # All fixture products have prices

    def test_statistics_rating_distribution(self, populated_db) -> None:
        """Statistics include a rating distribution."""
        stats = generate_statistics(populated_db)
        assert len(stats.rating_distribution) > 0
        total_rated = sum(stats.rating_distribution.values())
        assert total_rated == 100  # All reviews have ratings
