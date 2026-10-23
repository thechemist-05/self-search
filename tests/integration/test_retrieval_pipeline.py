"""
Integration tests for the complete Self Search Phase 4 retrieval pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from src.data.ingestion import ingest_from_fixture
from src.db.connection import get_connection
from src.db.migrations import run_migrations
from src.embeddings.embeddings import get_local_or_remote_embedding
from src.retrieval.filters import MetadataFilter
from src.retrieval.hybrid import HybridRetriever
from src.vector_store.vector_store import VectorStore


@pytest.fixture
def integrated_env(tmp_path: Path, fixture_data: dict):
    """Set up an isolated database and FAISS index populated with fixture data."""
    db_path = tmp_path / "test_shelfsense.db"
    conn = get_connection(db_path)
    run_migrations(conn)

    # Ingest 20 products and 100 reviews
    ingest_from_fixture(conn, fixture_data["products"], fixture_data["reviews"])

    # Build product vector store
    index_path = tmp_path / "products.index"
    product_store = VectorStore(str(index_path))

    products = conn.execute("SELECT id, title, description FROM products").fetchall()
    ids = [p[0] for p in products]
    texts = [f"{p[1]}\n{p[2]}" for p in products]
    vectors = [get_local_or_remote_embedding(t) for t in texts]
    product_store.add_vectors(vectors, "product", ids)

    retriever = HybridRetriever(conn=conn, product_store=product_store)

    yield {
        "conn": conn,
        "product_store": product_store,
        "retriever": retriever,
        "db_path": db_path,
    }

    conn.close()


def test_keyword_retrieval(integrated_env) -> None:
    retriever: HybridRetriever = integrated_env["retriever"]
    results = retriever.retrieve("fast charging cable", limit=3, mode="keyword")
    assert len(results) > 0
    top = results[0]
    assert "Cable" in top["title"]
    assert top["retrieval_source"] == "keyword"
    assert top["score"] > 0


def test_semantic_retrieval(integrated_env) -> None:
    retriever: HybridRetriever = integrated_env["retriever"]
    results = retriever.retrieve("comfortable wireless earbuds for music", limit=3, mode="semantic")
    assert len(results) > 0
    top = results[0]
    assert top["retrieval_source"] == "semantic"
    assert 0.0 < top["score"] <= 1.0
    assert "Earbuds" in top["title"] or "Speaker" in top["title"] or "Keyboard" in top["title"]


def test_hybrid_rrf_retrieval(integrated_env) -> None:
    retriever: HybridRetriever = integrated_env["retriever"]
    results = retriever.retrieve("hiking backpack 40l", limit=5, mode="hybrid")
    assert len(results) > 0
    top = results[0]
    assert top["retrieval_source"] == "hybrid"
    assert "Backpack" in top["title"]
    assert "rrf_score" in top
    assert "source_ranks" in top


def test_metadata_filtering(integrated_env) -> None:
    retriever: HybridRetriever = integrated_env["retriever"]
    # Filter by max price 30.0 and category Electronics
    filt = MetadataFilter(max_price=30.0, category="Electronics")
    results = retriever.retrieve("charger cable headphones", limit=5, filters=filt, mode="keyword")
    for r in results:
        assert r["price"] <= 30.0
        assert "Electronics" in r["category_name"]


def test_retrieve_with_evidence_and_id_consistency(integrated_env) -> None:
    retriever: HybridRetriever = integrated_env["retriever"]
    conn = integrated_env["conn"]

    products, evidence = retriever.retrieve_with_evidence(
        query="charging cable",
        limit=3,
        mode="hybrid",
        token_budget=1000,
    )
    assert len(products) > 0
    assert len(evidence.items) > 0
    assert evidence.total_tokens <= 1000

    # Verify ID consistency across all layers
    retrieved_product_ids = {p["id"] for p in products}
    for item in evidence.items:
        # Each evidence citation must belong to one of the retrieved products
        assert item.product_id in retrieved_product_ids
        # Evidence ID format
        assert item.evidence_id.startswith("R")
        # Check review exists in SQLite with same product_id and rating
        row = conn.execute(
            "SELECT product_id, rating FROM reviews WHERE id = ?",
            (item.review_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == item.product_id
        if item.rating is not None:
            assert row[1] == item.rating


def test_persistence_and_reopen(integrated_env, tmp_path) -> None:
    """Ensure the created index persists and can be reloaded without losing vectors or IDs."""
    store_path = integrated_env["product_store"]._index_path
    reloaded_store = VectorStore(str(store_path))
    assert reloaded_store.index is not None
    assert reloaded_store.index.ntotal == 20
    assert len(reloaded_store.id_map) == 20
    # Search reloaded store
    q_vec = get_local_or_remote_embedding("backpack")
    results = reloaded_store.search(q_vec, k=3)
    assert len(results) == 3
    for entity_type, entity_id, dist in results:
        assert entity_type == "product"
        assert 1 <= entity_id <= 20
