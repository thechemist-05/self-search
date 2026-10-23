"""Retrieval utilities for Self Search.

The high‑level ``search`` function:

1. Calls :func:`src.knowledge.embeddings.embed_texts` to obtain a query embedding.
2. Uses :func:`src.knowledge.vector_store.search_vectors` to retrieve the nearest
   vector IDs from the FAISS index.
3. Joins those IDs with the ``embedding_mapping`` SQLite table to resolve the
   original ``entity_type`` ("product" or "review") and ``entity_id``.
4. Fetches the full record from the appropriate table (``products`` or ``reviews``)
   and returns a list of dictionaries ready for consumption by the LLM agent.

Only the fields required for evidence generation are returned – for a product we
return ``product_id``, ``title`` and ``description``; for a review we return
``review_id``, ``product_id`` and ``text``.
"""

from __future__ import annotations

import sqlite3
from typing import List, Dict

from .embeddings import embed_texts
from .vector_store import search_vectors

# Relative import of the DB connection utilities.
from src.db.connection import get_connection, managed_connection


def _fetch_entities(conn: sqlite3.Connection, mapping_ids: List[int]) -> List[Dict]:
    """Resolve ``vector_id`` → entity data.

    ``mapping_ids`` are the IDs returned by FAISS. We first look them up in the
    ``embedding_mapping`` table, then fetch the corresponding rows from either
    ``products`` or ``reviews``.
    """
    if not mapping_ids:
        return []
    placeholders = ",".join(["?"] * len(mapping_ids))
    cur = conn.cursor()
    cur.execute(
        f"SELECT vector_id, entity_type, entity_id FROM embedding_mapping WHERE vector_id IN ({placeholders})",
        tuple(mapping_ids),
    )
    rows = cur.fetchall()
    results: List[Dict] = []
    for vector_id, entity_type, entity_id in rows:
        if entity_type == "product":
            cur.execute(
                "SELECT id as product_id, title, description FROM products WHERE id=?",
                (entity_id,),
            )
            prod = cur.fetchone()
            if prod:
                results.append({"vector_id": vector_id, "entity_type": "product", **dict(prod)})
        elif entity_type == "review":
            cur.execute(
                "SELECT id as review_id, product_id, text FROM reviews WHERE id=?",
                (entity_id,),
            )
            rev = cur.fetchone()
            if rev:
                results.append({"vector_id": vector_id, "entity_type": "review", **dict(rev)})
    # Preserve order of relevance as provided by FAISS (mapping_ids order)
    order_map = {vid: i for i, vid in enumerate(mapping_ids)}
    results.sort(key=lambda r: order_map.get(r["vector_id"], 0))
    return results


def search(query: str, top_k: int = 10) -> List[Dict]:
    """Return the *top_k* most relevant entities for ``query``.

    The function opens a managed SQLite connection (using the project's DB path –
    ``src.config.project_root`` points to the workspace root). It assumes the
    embedding dimension is compatible with the stored FAISS index.
    """
    # 1. Compute query embedding (single‑item list → 2‑D array)
    query_vec = embed_texts([query])
    if query_vec.size == 0:
        return []
    # 2. Search FAISS index
    vector_ids = search_vectors(query_vec, top_k=top_k)
    # 3. Resolve to concrete records
    with managed_connection("data/shelf_sense.db") as conn:  # path relative to project root
        results = _fetch_entities(conn, vector_ids)
    return results
