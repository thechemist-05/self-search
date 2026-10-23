# src/retrieval/retrieval.py
"""High‑level retrieval API for Self Search (Phase 4).

Provides:
1. Low-level vector retrieval via ``retrieve(query, k)`` for backward compatibility.
2. Full multi-stage hybrid product retrieval via ``retrieve_products(...)``.
3. Review evidence packaging via ``retrieve_with_evidence(...)``.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, List, Optional, Tuple

from src.config.settings import get_settings
from src.db.connection import get_connection
from src.embeddings.embeddings import get_local_or_remote_embedding
from src.evidence.evidence_pack import EvidencePack
from src.retrieval.filters import MetadataFilter
from src.retrieval.hybrid import HybridRetriever, RetrievalMode
from src.vector_store.vector_store import get_default_store, get_product_store


def retrieve(query: str, k: int = 5) -> List[Tuple[str, int, float]]:
    """Return the top‑k nearest neighbours for *query* directly from the default vector store.

    Parameters
    ----------
    query:
        The raw user query string.
    k:
        Number of neighbours to return (default 5).

    Returns
    -------
    List[Tuple[str, int, float]]
        Each tuple contains ``(entity_type, entity_id, distance)``.
    """
    store = get_default_store()
    dim = store.index.d if store.index else 64
    query_vec = get_local_or_remote_embedding(query, dim=dim)
    return store.search(query_vec, k)


def retrieve_products(
    query: str,
    limit: int = 10,
    filters: Optional[MetadataFilter] = None,
    mode: Optional[RetrievalMode] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> List[dict[str, Any]]:
    """Retrieve candidate products using the hybrid retrieval pipeline.

    Supports 'semantic', 'keyword', 'hybrid', and 'reranked_hybrid' modes.
    """
    settings = get_settings()
    close_conn = False
    if conn is None:
        conn = get_connection(settings.resolved_db_path)
        close_conn = True

    try:
        retriever = HybridRetriever(conn=conn, default_mode=mode)
        return retriever.retrieve(query=query, limit=limit, filters=filters, mode=mode)
    finally:
        if close_conn:
            conn.close()


def retrieve_with_evidence(
    query: str,
    limit: int = 10,
    filters: Optional[MetadataFilter] = None,
    mode: Optional[RetrievalMode] = None,
    max_reviews_per_product: int = 3,
    token_budget: int = 1500,
    conn: Optional[sqlite3.Connection] = None,
) -> Tuple[List[dict[str, Any]], EvidencePack]:
    """Retrieve candidate products and build an evidence pack from their reviews."""
    settings = get_settings()
    close_conn = False
    if conn is None:
        conn = get_connection(settings.resolved_db_path)
        close_conn = True

    try:
        retriever = HybridRetriever(conn=conn, default_mode=mode)
        return retriever.retrieve_with_evidence(
            query=query,
            limit=limit,
            filters=filters,
            mode=mode,
            max_reviews_per_product=max_reviews_per_product,
            token_budget=token_budget,
        )
    finally:
        if close_conn:
            conn.close()
