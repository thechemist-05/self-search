"""Retrieval module: semantic, keyword, hybrid, and reranking."""

from src.retrieval.filters import MetadataFilter
from src.retrieval.fusion import reciprocal_rank_fusion
from src.retrieval.hybrid import HybridRetriever, RetrievalMode
from src.retrieval.keyword import keyword_search_products, sanitize_fts5_query
from src.retrieval.rerank import CrossEncoderReranker
from src.retrieval.retrieval import retrieve, retrieve_products, retrieve_with_evidence
from src.retrieval.semantic import semantic_search_products

__all__ = [
    "CrossEncoderReranker",
    "HybridRetriever",
    "MetadataFilter",
    "RetrievalMode",
    "keyword_search_products",
    "reciprocal_rank_fusion",
    "retrieve",
    "retrieve_products",
    "retrieve_with_evidence",
    "sanitize_fts5_query",
    "semantic_search_products",
]
