"""
Hybrid product retrieval pipeline for Self Search (Phase 4).

Coordinates semantic retrieval (FAISS), keyword retrieval (SQLite FTS5 BM25),
metadata filtering, Reciprocal Rank Fusion (RRF), and optional Cross-Encoder reranking.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Literal, Optional

from src.config.settings import get_settings
from src.evidence.evidence_pack import EvidencePack, assemble_evidence_pack
from src.retrieval.constraints import type_phrases
from src.retrieval.filters import MetadataFilter
from src.retrieval.fusion import reciprocal_rank_fusion
from src.retrieval.keyword import keyword_search_products
from src.retrieval.rerank import CrossEncoderReranker
from src.retrieval.semantic import semantic_search_products
from src.vector_store.vector_store import VectorStore, get_product_store

RetrievalMode = Literal["semantic", "keyword", "hybrid", "reranked_hybrid"]


def _expand_with_type_synonyms(query: str, filters: Optional[MetadataFilter]) -> str:
    """Add the requested product types' synonyms to the keyword query.

    "sneakers" then also searches "shoe", so matching products rank by keyword
    relevance. This only affects ranking: the type constraint itself is
    enforced by ``MetadataFilter.matches``.
    """
    if not filters or not filters.product_types:
        return query
    extra = [phrase for ptype in filters.product_types for phrase in type_phrases(ptype)]
    return " ".join([query, *extra])


class HybridRetriever:
    """Configurable multi-stage hybrid product retriever."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        product_store: Optional[VectorStore] = None,
        default_mode: Optional[RetrievalMode] = None,
        reranker: Optional[CrossEncoderReranker] = None,
        rrf_k: int = 60,
        semantic_only_results: bool = False,
    ) -> None:
        """
        Args:
            semantic_only_results: Allow hybrid results that matched only the semantic
                channel when no product-type constraint is set. Off by default: the
                current product index is built from 64-d feature hashing (no embedding
                model is installed), so semantic-only hits are not meaningful. Enable once
                a real embedding model indexes the catalogue.
        """
        self.conn = conn
        self.product_store = product_store or get_product_store()
        settings = get_settings()
        self.default_mode = default_mode or settings.retrieval_mode
        self.reranker = reranker or CrossEncoderReranker()
        self.rrf_k = rrf_k
        self.semantic_only_results = semantic_only_results

    def retrieve(
        self,
        query: str,
        limit: int = 10,
        filters: Optional[MetadataFilter] = None,
        mode: Optional[RetrievalMode] = None,
    ) -> list[dict[str, Any]]:
        """Retrieve candidate products matching query and filters.

        Args:
            query: User search query string.
            limit: Maximum candidate products to return.
            filters: Optional metadata filtering constraints.
            mode: Retrieval mode ('semantic', 'keyword', 'hybrid', 'reranked_hybrid').
                  Defaults to self.default_mode.

        Returns:
            List of product dicts ordered by relevance score descending.
        """
        active_mode = mode or self.default_mode
        keyword_query = _expand_with_type_synonyms(query, filters)

        if active_mode == "keyword":
            return keyword_search_products(
                conn=self.conn,
                query=keyword_query,
                limit=limit,
                filters=filters,
            )

        if active_mode == "semantic":
            return semantic_search_products(
                conn=self.conn,
                query=query,
                limit=limit,
                filters=filters,
                store=self.product_store,
            )

        # Hybrid modes: fetch candidates from both keyword and semantic sources
        candidate_pool_limit = max(limit * 2, 10)
        kw_results = keyword_search_products(
            conn=self.conn,
            query=keyword_query,
            limit=candidate_pool_limit,
            filters=filters,
        )
        # Matching any word (OR) widens recall but also rewards products that match many
        # loose terms ("Knee High Boots ... Winter Shoes" for "running shoes"). A second
        # keyword list of products matching *all* of the user's own words gives them an
        # extra vote in the fusion.
        own_results = keyword_search_products(
            conn=self.conn,
            query=query,
            limit=candidate_pool_limit,
            filters=filters,
            match_all=True,
        )
        sem_results = semantic_search_products(
            conn=self.conn,
            query=query,
            limit=candidate_pool_limit,
            filters=filters,
            store=self.product_store,
        )

        ranked_lists = {}
        if kw_results:
            ranked_lists["keyword"] = kw_results
        if own_results:
            ranked_lists["keyword_own_words"] = own_results
        if sem_results:
            ranked_lists["semantic"] = sem_results

        fused = reciprocal_rank_fusion(ranked_lists=ranked_lists, k=self.rrf_k, limit=None)
        if not self.semantic_only_results:
            # Relevance floor: keep only products that matched the query's words (or, with a
            # product type, the type's synonyms). Semantic-only hits are dropped because the
            # offline index is feature-hashed, not semantic; fewer correct results beat padding.
            fused = [c for c in fused if {"keyword", "keyword_own_words"} & set(c.get("source_ranks") or {})]
        # Products containing every word the user typed come first (fused order is kept
        # within each tier), so synonym-heavy titles cannot outrank an exact match.
        fused.sort(key=lambda c: 0 if "keyword_own_words" in (c.get("source_ranks") or {}) else 1)
        fused = fused[: limit if active_mode == "hybrid" else candidate_pool_limit]

        if active_mode == "reranked_hybrid":
            return self.reranker.rerank(query=query, candidates=fused, limit=limit)

        return fused

    def retrieve_with_evidence(
        self,
        query: str,
        limit: int = 10,
        filters: Optional[MetadataFilter] = None,
        mode: Optional[RetrievalMode] = None,
        max_reviews_per_product: int = 3,
        token_budget: int = 1500,
        evidence_products: Optional[int] = None,
        max_words: int = 120,
    ) -> tuple[list[dict[str, Any]], EvidencePack]:
        """Retrieve candidate products and build an evidence pack from their reviews.

        Args:
            query: Search query.
            limit: Number of candidate products.
            filters: Optional metadata filters.
            mode: Optional retrieval mode.
            max_reviews_per_product: Max review snippets per product.
            token_budget: Token budget for the evidence pack.
            evidence_products: Gather evidence only for the first N products (default: all).
            max_words: Truncate each review snippet to this many words.

        Returns:
            Tuple of (products_list, evidence_pack).
        """
        products = self.retrieve(query=query, limit=limit, filters=filters, mode=mode)
        product_ids = [p["id"] for p in products][:evidence_products]
        evidence_pack = assemble_evidence_pack(
            conn=self.conn,
            product_ids=product_ids,
            query=query,
            max_reviews_per_product=max_reviews_per_product,
            token_budget=token_budget,
            max_words=max_words,
        )
        return products, evidence_pack
