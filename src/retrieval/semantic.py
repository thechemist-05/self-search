"""
Semantic retrieval for Self Search products using dense vector embeddings and FAISS.

Embeds queries and retrieves nearest neighbor candidate products,
hydrating candidates from SQLite and applying metadata filters.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from src.embeddings.embeddings import get_local_or_remote_embedding
from src.retrieval.filters import ESTIMATE_KEY, MetadataFilter
from src.vector_store.vector_store import VectorStore, get_product_store


def semantic_search_products(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    filters: Optional[MetadataFilter] = None,
    store: Optional[VectorStore] = None,
) -> list[dict[str, Any]]:
    """Search products using dense embedding similarity.

    Args:
        conn: SQLite database connection to hydrate product records.
        query: User search query string.
        limit: Maximum number of product candidates to return.
        filters: Optional metadata filters.
        store: Optional VectorStore instance (defaults to product_store).

    Returns:
        List of product dicts ordered by vector similarity (highest score first).
    """
    if not query or not query.strip():
        return []

    if store is None:
        store = get_product_store()

    if store.index is None or store.index.ntotal == 0:
        return []

    # Get query embedding
    dim = store.index.d
    query_vec = get_local_or_remote_embedding(query, dim=dim)

    # Overfetch candidates slightly if filters are present to satisfy limit after filtering
    fetch_k = limit * 3 if (filters and not filters.is_empty()) else limit
    if filters and filters.has_text_constraints():
        # Product-type/exclusion constraints can reject most neighbours; search the
        # whole index so every product that satisfies them is still considered.
        fetch_k = store.index.ntotal
    fetch_k = max(fetch_k, limit)

    raw_results = store.search(query_vec, k=fetch_k)
    if not raw_results:
        return []

    cursor = conn.cursor()
    candidates: list[dict[str, Any]] = []
    estimate_col = filters.estimate_column("p") if filters else None
    estimate_select = f", {estimate_col} AS price_estimate_inr" if estimate_col else ""

    for entity_type, entity_id, distance in raw_results:
        if entity_type != "product":
            continue

        row = cursor.execute(
            f"""
            SELECT
                p.id,
                p.source_id,
                p.external_id,
                p.title,
                p.description,
                p.category_id,
                p.brand_id,
                p.price,
                p.currency,
                p.avg_rating,
                p.rating_count,
                p.image_url,
                p.attributes_json,
                c.name AS category_name,
                b.name AS brand_name{estimate_select}
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            LEFT JOIN brands b ON p.brand_id = b.id
            WHERE p.id = ?
            """,
            (entity_id,),
        ).fetchone()

        if not row:
            continue

        # In L2 distance, smaller distance = closer vector.
        # Normalized similarity: 1 / (1 + distance)
        similarity = 1.0 / (1.0 + float(distance))

        product_dict = {
            "id": row[0],
            "source_id": row[1],
            "external_id": row[2],
            "title": row[3],
            "description": row[4],
            "category_id": row[5],
            "brand_id": row[6],
            "price": row[7],
            "currency": row[8],
            "avg_rating": row[9],
            "rating_count": row[10],
            "image_url": row[11],
            "attributes_json": row[12],
            "category_name": row[13],
            "brand_name": row[14],
            "score": similarity,
            "raw_distance": float(distance),
            "retrieval_source": "semantic",
        }
        if estimate_col:
            product_dict[ESTIMATE_KEY] = row[15]

        if filters and not filters.is_empty():
            if not filters.matches(product_dict):
                continue
        product_dict.pop(ESTIMATE_KEY, None)  # filtering only; never passed on

        candidates.append(product_dict)
        if len(candidates) >= limit:
            break

    # Sort descending by similarity score
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:limit]
