"""
Deduplication utilities for Self Search ingestion.

Detects and removes duplicate products and reviews based on
external IDs and content hashing.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Optional

import structlog

logger = structlog.get_logger("shelfsense.data.deduplication")


def product_exists(
    conn: sqlite3.Connection,
    source_id: int,
    external_id: str,
) -> Optional[int]:
    """Check if a product already exists for the given source and external ID.

    Args:
        conn: Active database connection.
        source_id: ID of the data source.
        external_id: External product identifier (e.g. parent_asin).

    Returns:
        Product ID if it exists, None otherwise.
    """
    row = conn.execute(
        "SELECT id FROM products WHERE source_id = ? AND external_id = ?",
        (source_id, external_id),
    ).fetchone()
    return row[0] if row else None


def review_exists(
    conn: sqlite3.Connection,
    source_id: int,
    external_id: str,
) -> Optional[int]:
    """Check if a review already exists for the given source and external ID.

    Args:
        conn: Active database connection.
        source_id: ID of the data source.
        external_id: External review identifier.

    Returns:
        Review ID if it exists, None otherwise.
    """
    row = conn.execute(
        "SELECT id FROM reviews WHERE source_id = ? AND external_id = ?",
        (source_id, external_id),
    ).fetchone()
    return row[0] if row else None


def compute_review_hash(user_id: str, product_asin: str, text: str) -> str:
    """Compute a deduplication hash for a review.

    Uses user_id + product_asin + text to create a unique identifier,
    so the same user reviewing the same product with the same text is detected.

    Args:
        user_id: Reviewer's user ID.
        product_asin: Product identifier.
        text: Review text.

    Returns:
        Hex digest of the SHA-256 hash.
    """
    content = f"{user_id}|{product_asin}|{text}".encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def count_products(conn: sqlite3.Connection, source_id: Optional[int] = None) -> int:
    """Count products, optionally filtered by source.

    Args:
        conn: Active database connection.
        source_id: Optional source ID filter.

    Returns:
        Number of products.
    """
    if source_id is not None:
        row = conn.execute(
            "SELECT COUNT(*) FROM products WHERE source_id = ?", (source_id,)
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) FROM products").fetchone()
    return row[0]


def count_reviews(conn: sqlite3.Connection, source_id: Optional[int] = None) -> int:
    """Count reviews, optionally filtered by source.

    Args:
        conn: Active database connection.
        source_id: Optional source ID filter.

    Returns:
        Number of reviews.
    """
    if source_id is not None:
        row = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE source_id = ?", (source_id,)
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()
    return row[0]
