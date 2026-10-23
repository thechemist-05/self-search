"""
Unit tests for Self Search Phase 4 retrieval components.
"""

from __future__ import annotations

import sqlite3
import pytest

from src.evaluation.retrieval_metrics import (
    evaluate_retrieval_run,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from src.evidence.evidence_pack import (
    EvidenceItem,
    EvidencePack,
    assemble_evidence_pack,
    estimate_tokens,
)
from src.retrieval.filters import MetadataFilter
from src.retrieval.fusion import reciprocal_rank_fusion
from src.retrieval.keyword import sanitize_fts5_query
from src.retrieval.rerank import CrossEncoderReranker


class TestMetadataFilter:
    """Tests for MetadataFilter logic and SQL generation."""

    def test_empty_filter(self) -> None:
        filt = MetadataFilter()
        assert filt.is_empty() is True
        where_sql, params = filt.build_sql_conditions()
        assert where_sql == ""
        assert params == []

    def test_filter_matches_product(self) -> None:
        filt = MetadataFilter(
            min_price=1000.0,
            max_price=5000.0,
            min_rating=4.0,
            category="electronics",
            brand="sony",
        )
        assert filt.is_empty() is False

        matching_prod = {
            "price": 2500.0,
            "currency": "INR",
            "avg_rating": 4.5,
            "category_name": "Consumer Electronics",
            "brand_name": "Sony Audio",
        }
        assert filt.matches(matching_prod) is True

        # Price too high
        assert filt.matches({**matching_prod, "price": 6000.0}) is False
        # Price too low
        assert filt.matches({**matching_prod, "price": 500.0}) is False
        # A USD source price is compared in INR (USD 25 = ₹2,077), never as 25 rupees
        assert filt.matches({**matching_prod, "price": 25.0, "currency": "USD"}) is True
        assert filt.matches({**matching_prod, "price": 5.0, "currency": "USD"}) is False
        # No currency: no usable price, so no budget is met
        assert filt.matches({**matching_prod, "currency": None}) is False
        # Rating too low
        assert filt.matches({**matching_prod, "avg_rating": 3.8}) is False
        # Category mismatch
        assert filt.matches({**matching_prod, "category_name": "Clothing"}) is False
        # Brand mismatch
        assert filt.matches({**matching_prod, "brand_name": "Bose"}) is False

    def test_sql_conditions_generation(self) -> None:
        import sqlite3

        filt = MetadataFilter(min_price=1000.0, max_price=5000.0, min_rating=4.2)
        where_sql, params = filt.build_sql_conditions(table_prefix="p")
        assert "UPPER(p.currency) = 'INR' THEN p.price" in where_sql  # normalised INR price
        assert "p.avg_rating >= ?" in where_sql
        assert params[-1] == 4.2 and 1000.0 in params and 5000.0 in params and 83.0815 in params
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE products (id INTEGER, price REAL, currency TEXT, avg_rating REAL)")
        conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?)", [
            (1, 2500.0, "INR", 4.5), (2, 25.0, "USD", 4.5), (3, 5.0, "USD", 4.5), (4, 2500.0, None, 4.5),
            (5, 2500.0, "INR", 4.0), (6, 30.0, "EUR", 4.5)])
        ids = {r[0] for r in conn.execute(f"SELECT p.id FROM products p WHERE {where_sql}", params)}
        assert ids == {1, 2}  # INR; USD 25 = ₹2,077; not USD 5 (₹415), no currency, low rating, EUR (no rate)


class TestFTS5Sanitization:
    """Tests for query sanitization for SQLite FTS5."""

    def test_empty_query(self) -> None:
        assert sanitize_fts5_query("") == ""
        assert sanitize_fts5_query("   ") == ""

    def test_special_characters_stripped(self) -> None:
        query = 'waterproof (backpack) & "lightweight"* : 100%'
        sanitized = sanitize_fts5_query(query)
        assert sanitized == '"waterproof"* OR "backpack"* OR "lightweight"*'
        for ch in "()&:%":
            assert ch not in sanitized
        # The only '*' is the prefix marker the sanitizer adds right after a quoted term.
        assert sanitized.count("*") == sanitized.count('"*')

    def test_or_joining(self) -> None:
        sanitized = sanitize_fts5_query("fast usb charger")
        assert sanitized == '"fast"* OR "usb" OR "charger"*'

    def test_stopwords_short_tokens_and_numbers_dropped(self) -> None:
        # "a" used to match the "A" of "USB-A" and rank a power bank first for a shoe query.
        q = "I need a shoe under 4000 rupees, which can be everyday use, sneakers."
        assert sanitize_fts5_query(q) == '"shoe"* OR "sneaker"*'
        assert sanitize_fts5_query("a an the of 4000") == ""

    def test_plural_insensitive_prefix(self) -> None:
        assert sanitize_fts5_query("shoes") == '"shoe"*'
        assert sanitize_fts5_query("batteries watches") == '"batter"* OR "watch"*'


class TestReciprocalRankFusion:
    """Tests for Reciprocal Rank Fusion algorithm."""

    def test_fusion_empty(self) -> None:
        assert reciprocal_rank_fusion({}) == []

    def test_fusion_ordering_and_ranks(self) -> None:
        list_a = [
            {"id": 1, "title": "Prod A"},
            {"id": 2, "title": "Prod B"},
        ]
        list_b = [
            {"id": 2, "title": "Prod B"},
            {"id": 3, "title": "Prod C"},
        ]
        # Document 2 is present in both lists (rank 2 in A, rank 1 in B)
        # Expected RRF score for Doc 2: 1/(60+2) + 1/(60+1)
        # Doc 1: 1/(60+1)
        # Doc 3: 1/(60+2)
        fused = reciprocal_rank_fusion({"sys_a": list_a, "sys_b": list_b}, k=60)
        assert len(fused) == 3
        # Doc 2 should be ranked 1st due to dual-system presence
        assert fused[0]["id"] == 2
        assert fused[0]["source_ranks"] == {"sys_a": 2, "sys_b": 1}
        assert fused[1]["id"] == 1
        assert fused[2]["id"] == 3

    def test_fusion_with_weights(self) -> None:
        list_a = [{"id": 1, "title": "Prod A"}]
        list_b = [{"id": 2, "title": "Prod B"}]
        # Heavily weight sys_b
        fused = reciprocal_rank_fusion(
            {"sys_a": list_a, "sys_b": list_b},
            weights={"sys_a": 1.0, "sys_b": 3.0},
        )
        assert fused[0]["id"] == 2


class TestEvidencePackaging:
    """Tests for token-budgeted review evidence packaging."""

    def test_token_estimation(self) -> None:
        assert estimate_tokens("") == 0
        assert estimate_tokens("hello world") >= 1

    def test_evidence_pack_formatting(self) -> None:
        item = EvidenceItem(
            evidence_id="R1",
            review_id=101,
            product_id=5,
            product_external_id="P005",
            title="Great backpack",
            text="Very durable and waterproof on trails.",
            rating=5.0,
            helpful_votes=12,
            verified=True,
            estimated_tokens=20,
        )
        pack = EvidencePack(query="backpack", items=[item], total_tokens=20, token_budget=500)
        formatted = pack.format_for_prompt()
        assert "[R1]" in formatted
        assert "P005" in formatted
        assert "Great backpack" in formatted
        assert "Very durable" in formatted

    def test_assemble_evidence_pack_respects_token_budget(self) -> None:
        # Create in-memory SQLite database with reviews
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                external_id TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE reviews (
                id INTEGER PRIMARY KEY,
                product_id INTEGER,
                rating REAL,
                title TEXT,
                text TEXT,
                helpful_votes INTEGER,
                verified_flag INTEGER
            )
            """
        )
        conn.execute("INSERT INTO products VALUES (1, 'PROD_1')")
        for i in range(10):
            conn.execute(
                "INSERT INTO reviews VALUES (?, 1, 5.0, 'Title', ?, 5, 1)",
                (i + 1, "A " * 50),  # Long text
            )
        conn.commit()

        # Strict token budget allowing only ~1-2 reviews
        pack = assemble_evidence_pack(conn, product_ids=[1], max_reviews_per_product=5, token_budget=80)
        assert len(pack.items) <= 2
        assert pack.total_tokens <= 80
        conn.close()


class TestCrossEncoderReranker:
    """Tests for CrossEncoder reranker behavior."""

    def test_reranker_disabled_by_default(self) -> None:
        reranker = CrossEncoderReranker(enabled=False)
        candidates = [{"id": 1, "score": 0.5}, {"id": 2, "score": 0.4}]
        result = reranker.rerank("query", candidates)
        assert len(result) == 2
        assert result[0]["id"] == 1
        assert result[0]["retrieval_source"] == "reranked_hybrid"

    def test_mock_model_reranking(self) -> None:
        reranker = CrossEncoderReranker(enabled=True)

        class MockCrossEncoder:
            def predict(self, pairs):
                # Return higher score for second candidate
                return [0.1, 0.9]

        reranker._model = MockCrossEncoder()
        reranker._load_attempted = True

        candidates = [
            {"id": 1, "title": "Item 1", "description": "Desc 1"},
            {"id": 2, "title": "Item 2", "description": "Desc 2"},
        ]
        reranked = reranker.rerank("query", candidates)
        assert reranked[0]["id"] == 2
        assert reranked[0]["rerank_score"] == 0.9


class TestRetrievalMetrics:
    """Tests for retrieval metrics calculation."""

    def test_recall_and_precision(self) -> None:
        retrieved = [1, 2, 3, 4, 5]
        relevant = {2, 5}
        assert recall_at_k(retrieved, relevant, k=5) == 1.0
        assert recall_at_k(retrieved, relevant, k=3) == 0.5  # only 2 is in top 3
        assert precision_at_k(retrieved, relevant, k=5) == 0.4  # 2 hits out of 5

    def test_reciprocal_rank(self) -> None:
        assert reciprocal_rank([1, 2, 3], {2}) == 0.5  # hit at rank 2
        assert reciprocal_rank([1, 2, 3], {1}) == 1.0  # hit at rank 1
        assert reciprocal_rank([1, 2, 3], {9}) == 0.0  # no hit

    def test_evaluate_retrieval_run(self) -> None:
        predictions = [
            ([1, 2, 3], {1}),  # RR = 1.0, Recall@3 = 1.0
            ([3, 2, 1], {1}),  # RR = 0.3333, Recall@3 = 1.0
        ]
        metrics = evaluate_retrieval_run(predictions, k_values=[1, 3])
        assert metrics["Recall@3"] == 1.0
        assert metrics["MRR"] == round((1.0 + 1 / 3) / 2, 4)
