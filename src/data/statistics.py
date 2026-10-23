"""
Data statistics and quality report generation for Self Search.

Generates comprehensive statistics about the ingested data including
counts, distributions, coverage, and quality metrics.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger("shelfsense.data.statistics")


@dataclass
class DataStatistics:
    """Container for data statistics."""

    total_products: int = 0
    total_reviews: int = 0
    total_categories: int = 0
    total_brands: int = 0
    products_with_price: int = 0
    products_with_description: int = 0
    products_with_image: int = 0
    products_with_attributes: int = 0
    reviews_with_text: int = 0
    reviews_verified: int = 0
    avg_reviews_per_product: float = 0.0
    avg_rating: float = 0.0
    price_stats: dict[str, Any] = field(default_factory=dict)
    rating_distribution: dict[str, int] = field(default_factory=dict)
    category_counts: dict[str, int] = field(default_factory=dict)
    top_brands: list[tuple[str, int]] = field(default_factory=list)
    missing_data_summary: dict[str, int] = field(default_factory=dict)


def generate_statistics(conn: sqlite3.Connection) -> DataStatistics:
    """Generate comprehensive statistics from the database.

    Args:
        conn: Active database connection.

    Returns:
        DataStatistics with all computed metrics.
    """
    stats = DataStatistics()

    # Basic counts
    stats.total_products = _scalar(conn, "SELECT COUNT(*) FROM products")
    stats.total_reviews = _scalar(conn, "SELECT COUNT(*) FROM reviews")
    stats.total_categories = _scalar(conn, "SELECT COUNT(*) FROM categories")
    stats.total_brands = _scalar(conn, "SELECT COUNT(*) FROM brands")

    if stats.total_products == 0:
        logger.warning("no_products_found")
        return stats

    # Product coverage
    stats.products_with_price = _scalar(
        conn, "SELECT COUNT(*) FROM products WHERE price IS NOT NULL AND price > 0"
    )
    stats.products_with_description = _scalar(
        conn, "SELECT COUNT(*) FROM products WHERE description IS NOT NULL AND description != ''"
    )
    stats.products_with_image = _scalar(
        conn, "SELECT COUNT(*) FROM products WHERE image_url IS NOT NULL AND image_url != ''"
    )
    stats.products_with_attributes = _scalar(
        conn, "SELECT COUNT(*) FROM products WHERE attributes_json IS NOT NULL"
    )

    # Review coverage
    stats.reviews_with_text = _scalar(
        conn, "SELECT COUNT(*) FROM reviews WHERE text IS NOT NULL AND text != ''"
    )
    stats.reviews_verified = _scalar(
        conn, "SELECT COUNT(*) FROM reviews WHERE verified_flag = 1"
    )

    # Averages
    if stats.total_products > 0:
        stats.avg_reviews_per_product = round(
            stats.total_reviews / stats.total_products, 2
        )

    avg_row = conn.execute("SELECT AVG(avg_rating) FROM products WHERE avg_rating IS NOT NULL").fetchone()
    stats.avg_rating = round(avg_row[0], 2) if avg_row and avg_row[0] else 0.0

    # Price statistics
    price_row = conn.execute(
        "SELECT MIN(price), MAX(price), AVG(price) FROM products "
        "WHERE price IS NOT NULL AND price > 0"
    ).fetchone()
    if price_row and price_row[0] is not None:
        stats.price_stats = {
            "min": round(price_row[0], 2),
            "max": round(price_row[1], 2),
            "avg": round(price_row[2], 2),
        }

    # Rating distribution (reviews)
    for row in conn.execute(
        "SELECT CAST(rating AS INTEGER) as r, COUNT(*) FROM reviews "
        "WHERE rating IS NOT NULL GROUP BY r ORDER BY r"
    ):
        stats.rating_distribution[str(row[0])] = row[1]

    # Category counts
    for row in conn.execute(
        "SELECT c.name, COUNT(p.id) FROM categories c "
        "LEFT JOIN products p ON p.category_id = c.id "
        "GROUP BY c.id ORDER BY COUNT(p.id) DESC"
    ):
        stats.category_counts[row[0]] = row[1]

    # Top brands
    stats.top_brands = [
        (row[0], row[1])
        for row in conn.execute(
            "SELECT b.name, COUNT(p.id) FROM brands b "
            "JOIN products p ON p.brand_id = b.id "
            "GROUP BY b.id ORDER BY COUNT(p.id) DESC LIMIT 20"
        )
    ]

    # Missing data summary
    stats.missing_data_summary = {
        "products_without_price": stats.total_products - stats.products_with_price,
        "products_without_description": stats.total_products - stats.products_with_description,
        "products_without_image": stats.total_products - stats.products_with_image,
        "reviews_without_text": stats.total_reviews - stats.reviews_with_text,
    }

    logger.info(
        "statistics_generated",
        products=stats.total_products,
        reviews=stats.total_reviews,
        categories=stats.total_categories,
        brands=stats.total_brands,
    )

    return stats


def format_report(stats: DataStatistics) -> str:
    """Format statistics into a human-readable markdown report.

    Args:
        stats: DataStatistics to format.

    Returns:
        Markdown string.
    """
    lines = [
        "# Self Search Data Statistics Report",
        "",
        "## Overview",
        "",
        f"| Metric | Count |",
        f"|--------|-------|",
        f"| Total products | {stats.total_products:,} |",
        f"| Total reviews | {stats.total_reviews:,} |",
        f"| Categories | {stats.total_categories} |",
        f"| Brands | {stats.total_brands:,} |",
        f"| Avg reviews per product | {stats.avg_reviews_per_product} |",
        f"| Avg product rating | {stats.avg_rating} |",
        "",
        "## Product Data Coverage",
        "",
        f"| Field | Has data | Missing | Coverage |",
        f"|-------|----------|---------|----------|",
    ]

    total = stats.total_products if stats.total_products > 0 else 1
    for label, has_count in [
        ("Price", stats.products_with_price),
        ("Description", stats.products_with_description),
        ("Image URL", stats.products_with_image),
        ("Attributes", stats.products_with_attributes),
    ]:
        missing = total - has_count
        pct = round(has_count / total * 100, 1) if total > 0 else 0
        lines.append(f"| {label} | {has_count:,} | {missing:,} | {pct}% |")

    lines.extend([
        "",
        "## Review Data Coverage",
        "",
        f"| Metric | Count | Percentage |",
        f"|--------|-------|------------|",
        f"| With text | {stats.reviews_with_text:,} | "
        f"{round(stats.reviews_with_text / max(stats.total_reviews, 1) * 100, 1)}% |",
        f"| Verified | {stats.reviews_verified:,} | "
        f"{round(stats.reviews_verified / max(stats.total_reviews, 1) * 100, 1)}% |",
    ])

    if stats.price_stats:
        lines.extend([
            "",
            "## Price Statistics",
            "",
            f"| Stat | Value |",
            f"|------|-------|",
            f"| Min | ${stats.price_stats['min']:,.2f} |",
            f"| Max | ${stats.price_stats['max']:,.2f} |",
            f"| Avg | ${stats.price_stats['avg']:,.2f} |",
        ])

    if stats.rating_distribution:
        lines.extend([
            "",
            "## Review Rating Distribution",
            "",
            f"| Stars | Count |",
            f"|-------|-------|",
        ])
        for star, count in sorted(stats.rating_distribution.items()):
            lines.append(f"| {star} ★ | {count:,} |")

    if stats.category_counts:
        lines.extend([
            "",
            "## Products by Category",
            "",
            f"| Category | Products |",
            f"|----------|----------|",
        ])
        for cat, count in stats.category_counts.items():
            lines.append(f"| {cat} | {count:,} |")

    if stats.top_brands:
        lines.extend([
            "",
            "## Top 20 Brands",
            "",
            f"| Brand | Products |",
            f"|-------|----------|",
        ])
        for brand, count in stats.top_brands:
            lines.append(f"| {brand} | {count:,} |")

    return "\n".join(lines)


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    """Execute a query that returns a single integer value."""
    row = conn.execute(sql).fetchone()
    return row[0] if row and row[0] is not None else 0
