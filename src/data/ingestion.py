"""
Data ingestion pipeline for Self Search.

Downloads product and review data from HuggingFace datasets,
validates, transforms, deduplicates, and loads into SQLite.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from tqdm import tqdm

from src.data.deduplication import compute_review_hash, product_exists, review_exists
from src.data.source_registry import register_source
from src.data.validation import ValidationResult, clean_text, parse_price, validate_product, validate_review
from src.db.connection import transaction

logger = structlog.get_logger("shelfsense.data.ingestion")

# Default configuration
DEFAULT_CATEGORIES = ["Electronics", "Sports_and_Outdoors"]
MAX_PRODUCTS_PER_CATEGORY = 10_000
MAX_REVIEWS_PER_CATEGORY = 100_000
BATCH_SIZE = 500


def ingest_from_huggingface(
    conn: sqlite3.Connection,
    categories: Optional[list[str]] = None,
    max_products: int = MAX_PRODUCTS_PER_CATEGORY,
    max_reviews: int = MAX_REVIEWS_PER_CATEGORY,
    trust_remote_code: bool = True,
) -> dict[str, Any]:
    """Run the full ingestion pipeline for Amazon Reviews 2023.

    1. Register the data source
    2. For each category: load items, validate, transform, insert products
    3. For each category: load reviews, validate, link to products, insert

    Args:
        conn: Active database connection with schema already created.
        categories: List of category names to ingest. Defaults to Electronics + Sports_and_Outdoors.
        max_products: Maximum products per category.
        max_reviews: Maximum reviews per category.
        trust_remote_code: Whether to trust remote code in HuggingFace datasets.

    Returns:
        Statistics dict with counts and timings.
    """
    from datasets import load_dataset

    if categories is None:
        categories = DEFAULT_CATEGORIES

    stats: dict[str, Any] = {
        "start_time": datetime.now(timezone.utc).isoformat(),
        "categories": {},
        "totals": {"products": 0, "reviews": 0, "skipped_products": 0, "skipped_reviews": 0},
    }

    # Register the data source
    source_id = register_source(
        conn,
        name="amazon-reviews-2023",
        url="https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023",
        license="Research use; cite Hou et al. (2024)",
        notes=f"Categories: {', '.join(categories)}. Max {max_products} products, "
              f"{max_reviews} reviews per category.",
    )

    for category in categories:
        logger.info("ingesting_category", category=category)
        cat_stats = {"products_inserted": 0, "products_skipped": 0,
                     "reviews_inserted": 0, "reviews_skipped": 0}
        cat_start = time.time()

        # Ensure category exists in the categories table
        category_id = _ensure_category(conn, category)

        # -- Ingest items (products) --
        logger.info("loading_items", category=category)
        try:
            item_dataset = load_dataset(
                "McAuley-Lab/Amazon-Reviews-2023",
                f"raw_meta_{category}",
                split="full",
                trust_remote_code=trust_remote_code,
            )
        except Exception as e:
            logger.error("failed_to_load_items", category=category, error=str(e))
            stats["categories"][category] = {"error": str(e)}
            continue

        product_count = 0
        product_batch: list[tuple] = []

        for item in tqdm(item_dataset, desc=f"Products [{category}]", total=min(max_products, len(item_dataset))):
            if product_count >= max_products:
                break

            record = _transform_product(item, source_id, category_id)
            validation = validate_product(record)

            if not validation.is_valid:
                cat_stats["products_skipped"] += 1
                continue

            # Check for duplicates
            if product_exists(conn, source_id, record["external_id"]):
                cat_stats["products_skipped"] += 1
                continue

            product_batch.append(_product_to_row(record))
            product_count += 1

            if len(product_batch) >= BATCH_SIZE:
                _insert_product_batch(conn, product_batch)
                product_batch = []

        if product_batch:
            _insert_product_batch(conn, product_batch)

        cat_stats["products_inserted"] = product_count
        logger.info("items_ingested", category=category, count=product_count)

        # Build a lookup for product external_id -> internal id
        asin_to_id = _build_asin_lookup(conn, source_id)

        # -- Ingest reviews --
        logger.info("loading_reviews", category=category)
        try:
            review_dataset = load_dataset(
                "McAuley-Lab/Amazon-Reviews-2023",
                f"raw_review_{category}",
                split="full",
                trust_remote_code=trust_remote_code,
            )
        except Exception as e:
            logger.error("failed_to_load_reviews", category=category, error=str(e))
            cat_stats["review_error"] = str(e)
            stats["categories"][category] = cat_stats
            continue

        review_count = 0
        review_batch: list[tuple] = []
        seen_hashes: set[str] = set()

        for rev in tqdm(review_dataset, desc=f"Reviews [{category}]", total=min(max_reviews, len(review_dataset))):
            if review_count >= max_reviews:
                break

            parent_asin = rev.get("parent_asin", "")
            product_id = asin_to_id.get(parent_asin)

            if product_id is None:
                cat_stats["reviews_skipped"] += 1
                continue

            record = _transform_review(rev, product_id, source_id)
            validation = validate_review(record)

            if not validation.is_valid:
                cat_stats["reviews_skipped"] += 1
                continue

            # Content-based deduplication
            review_hash = compute_review_hash(
                rev.get("user_id", ""),
                parent_asin,
                record.get("text", ""),
            )
            if review_hash in seen_hashes:
                cat_stats["reviews_skipped"] += 1
                continue
            seen_hashes.add(review_hash)

            # DB-level dedup
            ext_id = record.get("external_id", "")
            if ext_id and review_exists(conn, source_id, ext_id):
                cat_stats["reviews_skipped"] += 1
                continue

            review_batch.append(_review_to_row(record))
            review_count += 1

            if len(review_batch) >= BATCH_SIZE:
                _insert_review_batch(conn, review_batch)
                review_batch = []

        if review_batch:
            _insert_review_batch(conn, review_batch)

        cat_stats["reviews_inserted"] = review_count
        cat_stats["duration_seconds"] = round(time.time() - cat_start, 1)
        stats["categories"][category] = cat_stats

        stats["totals"]["products"] += cat_stats["products_inserted"]
        stats["totals"]["reviews"] += cat_stats["reviews_inserted"]
        stats["totals"]["skipped_products"] += cat_stats["products_skipped"]
        stats["totals"]["skipped_reviews"] += cat_stats["reviews_skipped"]

        logger.info(
            "category_complete",
            category=category,
            products=cat_stats["products_inserted"],
            reviews=cat_stats["reviews_inserted"],
            duration=cat_stats["duration_seconds"],
        )

    stats["end_time"] = datetime.now(timezone.utc).isoformat()
    return stats


def ingest_from_fixture(
    conn: sqlite3.Connection,
    products: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    source_name: str = "test-fixture",
) -> dict[str, Any]:
    """Ingest products and reviews from in-memory fixture data.

    Used for testing. Follows the same validation and dedup logic
    as the full pipeline, but without HuggingFace download.

    Args:
        conn: Active database connection with schema created.
        products: List of product dicts with keys matching the schema.
        reviews: List of review dicts (must include 'product_external_id').
        source_name: Name for the data source registry entry.

    Returns:
        Statistics dict with counts.
    """
    source_id = register_source(conn, name=source_name)
    stats = {"products_inserted": 0, "products_skipped": 0,
             "reviews_inserted": 0, "reviews_skipped": 0}

    # Insert products
    for prod in products:
        prod["source_id"] = source_id
        if not prod.get("external_id"):
            prod["external_id"] = prod.get("id", "")

        validation = validate_product(prod)
        if not validation.is_valid:
            stats["products_skipped"] += 1
            continue

        if product_exists(conn, source_id, str(prod["external_id"])):
            stats["products_skipped"] += 1
            continue

        # Ensure category
        cat_id = None
        if prod.get("category"):
            cat_id = _ensure_category(conn, prod["category"])

        # Ensure brand
        brand_id = None
        if prod.get("brand"):
            brand_id = _ensure_brand(conn, prod["brand"])

        with transaction(conn) as cur:
            cur.execute(
                "INSERT INTO products "
                "(source_id, external_id, title, description, category_id, brand_id, "
                "price, currency, avg_rating, rating_count, image_url, attributes_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    str(prod["external_id"]),
                    prod.get("title", ""),
                    prod.get("description", ""),
                    cat_id,
                    brand_id,
                    prod.get("price"),
                    prod.get("currency", "USD"),
                    prod.get("avg_rating"),
                    prod.get("rating_count", 0),
                    prod.get("image_url"),
                    json.dumps(prod.get("attributes", {})) if prod.get("attributes") else None,
                ),
            )
        stats["products_inserted"] += 1

    # Build asin lookup
    asin_to_id = _build_asin_lookup(conn, source_id)

    # Insert reviews
    seen_hashes: set[str] = set()
    for rev in reviews:
        product_ext_id = str(rev.get("product_external_id", rev.get("product_id", "")))
        product_id = asin_to_id.get(product_ext_id)

        if product_id is None:
            stats["reviews_skipped"] += 1
            continue

        rev["product_id"] = product_id
        rev["source_id"] = source_id

        validation = validate_review(rev)
        if not validation.is_valid:
            stats["reviews_skipped"] += 1
            continue

        # Dedup by content hash
        review_hash = compute_review_hash(
            str(rev.get("user_id", "")),
            product_ext_id,
            str(rev.get("text", "")),
        )
        if review_hash in seen_hashes:
            stats["reviews_skipped"] += 1
            continue
        seen_hashes.add(review_hash)

        ext_id = str(rev.get("external_id", review_hash[:16]))
        if review_exists(conn, source_id, ext_id):
            stats["reviews_skipped"] += 1
            continue

        with transaction(conn) as cur:
            cur.execute(
                "INSERT INTO reviews "
                "(product_id, source_id, external_id, rating, title, text, "
                "review_date, helpful_votes, verified_flag) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    product_id,
                    source_id,
                    ext_id,
                    rev.get("rating"),
                    rev.get("title", ""),
                    rev.get("text", ""),
                    rev.get("review_date"),
                    rev.get("helpful_votes", 0),
                    1 if rev.get("verified", False) else 0,
                ),
            )
        stats["reviews_inserted"] += 1

    return stats


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _transform_product(
    item: dict[str, Any], source_id: int, category_id: int
) -> dict[str, Any]:
    """Transform a raw Amazon item record into our product schema."""
    # Parse price
    price = parse_price(item.get("price"))

    # Extract brand from store field
    brand = item.get("store", "")

    # Build description from description list
    description = clean_text(item.get("description", ""))

    # Build attributes from features
    features = item.get("features", [])
    details = item.get("details", "")
    attributes = {}
    if features:
        attributes["features"] = features if isinstance(features, list) else [features]
    if details:
        if isinstance(details, str):
            try:
                attributes["details"] = json.loads(details)
            except json.JSONDecodeError:
                attributes["details"] = details
        else:
            attributes["details"] = details

    # Get best image URL
    images = item.get("images", {})
    image_url = None
    if isinstance(images, dict):
        # Prefer hi_res, then large, then thumb
        for key in ["hi_res", "large", "thumb"]:
            img_list = images.get(key, [])
            if img_list and isinstance(img_list, list):
                for img in img_list:
                    if img:
                        image_url = img
                        break
                if image_url:
                    break
    elif isinstance(images, list) and images:
        image_url = images[0] if isinstance(images[0], str) else None

    return {
        "source_id": source_id,
        "external_id": item.get("parent_asin", ""),
        "title": clean_text(item.get("title", "")),
        "description": description,
        "category_id": category_id,
        "brand": brand,
        "price": price,
        "currency": "USD",
        "avg_rating": item.get("average_rating"),
        "rating_count": item.get("rating_number", 0),
        "image_url": image_url,
        "attributes_json": json.dumps(attributes) if attributes else None,
    }


def _transform_review(
    rev: dict[str, Any], product_id: int, source_id: int
) -> dict[str, Any]:
    """Transform a raw Amazon review record into our review schema."""
    # Convert Unix timestamp to ISO date
    timestamp = rev.get("timestamp")
    review_date = None
    if timestamp:
        try:
            review_date = datetime.fromtimestamp(
                int(timestamp) / 1000, tz=timezone.utc
            ).isoformat()
        except (ValueError, TypeError, OSError):
            review_date = None

    # Build external_id from user_id + asin
    user_id = rev.get("user_id", "")
    asin = rev.get("parent_asin", rev.get("asin", ""))
    ext_id = f"{user_id}_{asin}_{timestamp}" if user_id and asin else ""

    return {
        "product_id": product_id,
        "source_id": source_id,
        "external_id": ext_id,
        "rating": rev.get("rating"),
        "title": clean_text(rev.get("title", "")),
        "text": clean_text(rev.get("text", "")),
        "review_date": review_date,
        "helpful_votes": rev.get("helpful_vote", 0),
        "verified": rev.get("verified_purchase", False),
        "parent_asin": asin,
    }


def _product_to_row(record: dict[str, Any]) -> tuple:
    """Convert a product dict to a tuple for batch insertion."""
    brand_id = None  # Will be resolved during batch insert
    return (
        record["source_id"],
        str(record["external_id"]),
        record.get("title", ""),
        record.get("description", ""),
        record.get("category_id"),
        brand_id,
        record.get("price"),
        record.get("currency", "USD"),
        record.get("avg_rating"),
        record.get("rating_count", 0),
        record.get("image_url"),
        record.get("attributes_json"),
        record.get("brand", ""),
    )


def _review_to_row(record: dict[str, Any]) -> tuple:
    """Convert a review dict to a tuple for batch insertion."""
    return (
        record["product_id"],
        record["source_id"],
        record.get("external_id", ""),
        record.get("rating"),
        record.get("title", ""),
        record.get("text", ""),
        record.get("review_date"),
        record.get("helpful_votes", 0),
        1 if record.get("verified", False) else 0,
    )


def _insert_product_batch(conn: sqlite3.Connection, batch: list[tuple]) -> None:
    """Insert a batch of product rows, resolving brands along the way."""
    with transaction(conn) as cur:
        for row in batch:
            *fields, brand_name = row
            brand_id = None
            if brand_name:
                brand_id = _ensure_brand(conn, brand_name)
            # Replace the brand_id (index 5) in the fields
            fields[5] = brand_id
            cur.execute(
                "INSERT OR IGNORE INTO products "
                "(source_id, external_id, title, description, category_id, brand_id, "
                "price, currency, avg_rating, rating_count, image_url, attributes_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(fields),
            )


def _insert_review_batch(conn: sqlite3.Connection, batch: list[tuple]) -> None:
    """Insert a batch of review rows."""
    with transaction(conn) as cur:
        cur.executemany(
            "INSERT OR IGNORE INTO reviews "
            "(product_id, source_id, external_id, rating, title, text, "
            "review_date, helpful_votes, verified_flag) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            batch,
        )


def _ensure_category(conn: sqlite3.Connection, name: str) -> int:
    """Get or create a category and return its ID."""
    row = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
    if row:
        return row[0]
    with transaction(conn) as cur:
        cur.execute("INSERT INTO categories (name) VALUES (?)", (name,))
        return cur.lastrowid


def _ensure_brand(conn: sqlite3.Connection, name: str) -> int:
    """Get or create a brand and return its ID."""
    row = conn.execute("SELECT id FROM brands WHERE name = ?", (name,)).fetchone()
    if row:
        return row[0]
    with transaction(conn) as cur:
        cur.execute("INSERT OR IGNORE INTO brands (name) VALUES (?)", (name,))
        # Fetch again in case of race
        row2 = conn.execute("SELECT id FROM brands WHERE name = ?", (name,)).fetchone()
        return row2[0]


def _build_asin_lookup(conn: sqlite3.Connection, source_id: int) -> dict[str, int]:
    """Build a mapping from external_id (parent_asin) to internal product ID."""
    rows = conn.execute(
        "SELECT external_id, id FROM products WHERE source_id = ?", (source_id,)
    ).fetchall()
    return {row[0]: row[1] for row in rows}
