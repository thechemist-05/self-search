"""
Unit tests for Phase 9 backend presentation pieces: DB-backed product cards,
result ordering, evidence snippets and generation-mode reporting.
"""

from __future__ import annotations

import json

import pytest

from src.agent.presentation import ProductPresenter, evidence_snippets, order_result_ids
from src.evidence.evidence_pack import EvidenceItem, EvidencePack
from src.llm.ollama import OllamaConnectionError, OllamaError
from src.llm.provider import MockLLMProvider
from src.llm.schemas import ProductRecommendation, StructuredAnswer
from src.llm.service import LLMService


def _answer(*product_ids: int, title: str = "LLM TITLE") -> StructuredAnswer:
    return StructuredAnswer(
        summary="s",
        recommendations=[ProductRecommendation(product_id=pid, title=title) for pid in product_ids],
    )


class TestOrdering:
    def test_recommended_first_then_rest(self) -> None:
        ordered, rec = order_result_ids([1, 2, 3, 4], _answer(3, 1))
        assert ordered == [3, 1, 2, 4] and rec == {1, 3}

    def test_llm_cannot_add_products(self) -> None:
        ordered, rec = order_result_ids([1, 2], _answer(999, 2))
        assert ordered == [2, 1] and rec == {2}

    def test_no_answer(self) -> None:
        assert order_result_ids([5, 5, 6], None) == ([5, 6], set())


class TestProductCards:
    def test_cards_come_from_database_in_requested_order(self, personal_db) -> None:
        personal_db.execute(
            "UPDATE products SET attributes_json = ?, image_url = 'https://example.com/x.jpg', "
            "rating_count = 120, currency = 'USD' WHERE id = 3",
            (json.dumps({"features": ["Waterproof", "40L"], "weight": "0.8 kg"}),),
        )
        personal_db.execute(
            "INSERT INTO reviews (product_id, source_id, external_id, rating, text) VALUES (3, 1, 'R1', 5, 'great')"
        )
        cards = ProductPresenter(personal_db).cards([3, 1], recommended_ids={3}, retrieval_ranks={1: 1, 3: 2})

        assert [c.id for c in cards] == [3, 1]
        c = cards[0]
        # Source price USD 49.99 is shown only as its documented INR conversion.
        assert (c.title, c.brand, c.category, c.price, c.currency) == (
            "Hiking Backpack", "TrailMaster", "Sports_and_Outdoors", 4153.0, "INR")
        assert c.price_status == "converted" and c.price_conversion.from_currency == "USD"
        assert (c.price_conversion.rate, c.price_conversion.rate_date) == (83.0815, "2023-09-29")
        assert "49.99" not in c.model_dump_json()
        assert (c.catalog_rating, c.catalog_rating_count, c.local_review_count) == (4.3, 120, 1)
        assert c.features == ["Waterproof", "40L"] and c.attributes == {"weight": "0.8 kg"}
        assert c.image_url == "https://example.com/x.jpg"
        assert c.recommended is True and c.retrieval_rank == 2
        assert cards[1].recommended is False and cards[1].local_review_count == 0
        assert c.aspects == []  # Phase 2 not implemented: nothing synthesised

    def test_malformed_attributes_and_unknown_ids(self, personal_db) -> None:
        personal_db.execute("UPDATE products SET attributes_json = 'not json' WHERE id = 2")
        cards = ProductPresenter(personal_db).cards([2, 999])
        assert [c.id for c in cards] == [2]
        assert cards[0].features == [] and cards[0].attributes == {}

    def test_aspects_passed_through_only_when_stored(self, personal_db) -> None:
        personal_db.execute(
            "INSERT INTO product_aspect_summary (product_id, aspect, positive_count, negative_count, neutral_count) "
            "VALUES (1, 'battery', 4, 1, 0)"
        )
        cards = ProductPresenter(personal_db).cards([1, 2])
        assert cards[0].aspects == [{"aspect": "battery", "positive_count": 4, "negative_count": 1, "neutral_count": 0}]
        assert cards[1].aspects == []

    def test_detail_lists_local_reviews_most_helpful_first(self, personal_db) -> None:
        personal_db.executemany(
            "INSERT INTO reviews (product_id, source_id, external_id, rating, text, helpful_votes, verified_flag) "
            "VALUES (1, 1, ?, ?, ?, ?, ?)",
            [("A", 4, "ok", 2, 0), ("B", 5, "best", 9, 1)],
        )
        detail = ProductPresenter(personal_db).detail(1)
        assert detail is not None
        assert detail["product"]["title"] == "Wireless Earbuds"
        assert [r["text"] for r in detail["reviews"]] == ["best", "ok"]
        assert detail["reviews"][0]["verified"] is True
        assert ProductPresenter(personal_db).detail(999) is None


class TestEvidenceSnippets:
    def test_snippets_mirror_the_pack_exactly(self) -> None:
        items = [
            EvidenceItem("R1", 10, 1, "P001", "Light", "Very light bag", 5.0, 3, True),
            EvidenceItem("R2", 11, 2, "P002", "", "Battery died", 2.0, 0, False),
        ]
        snippets = evidence_snippets(EvidencePack(query="q", items=items))
        assert [(s.evidence_id, s.review_id, s.product_id, s.text, s.rating) for s in snippets] == [
            ("R1", 10, 1, "Very light bag", 5.0), ("R2", 11, 2, "Battery died", 2.0),
        ]
        assert snippets[1].title is None and snippets[0].verified is True
        assert evidence_snippets(None) == []


_VALID = json.dumps({"summary": "ok", "recommendations": [], "insufficient_evidence": False})


class TestGenerationMode:
    PRODUCTS = [{"id": 1, "title": "Bag", "price": 10.0}]

    def test_llm_mode(self) -> None:
        _, info = LLMService(MockLLMProvider(responses=[_VALID])).generate_answer_with_mode("q", self.PRODUCTS)
        assert (info.mode, info.attempts, info.model) == ("llm", 1, "mock-llm")

    def test_llm_after_retry_counts_attempts(self) -> None:
        svc = LLMService(MockLLMProvider(responses=["junk", _VALID]))
        assert svc.generate_answer_with_mode("q", self.PRODUCTS)[1].attempts == 2

    def test_fallback_after_invalid_output(self) -> None:
        svc = LLMService(MockLLMProvider(default_response="junk"), max_retries=2)
        answer, info = svc.generate_answer_with_mode("q", self.PRODUCTS)
        assert (info.mode, info.reason) == ("fallback", "invalid_output_after_retries")
        assert answer.recommendations[0].product_id == 1  # safe template from real products

    @pytest.mark.parametrize("exc,reason", [
        (OllamaConnectionError("down"), "provider_unavailable"),
        (OllamaError("500"), "provider_error"),
        (RuntimeError("boom"), "unexpected_error"),
    ])
    def test_fallback_after_model_error_mid_request(self, exc, reason) -> None:
        def raise_(_messages):
            raise exc

        # The provider reports itself available; the failure happens during generation.
        provider = MockLLMProvider(handler=raise_, available=True)
        _, info = LLMService(provider).generate_answer_with_mode("q", self.PRODUCTS)
        assert (info.mode, info.reason) == ("fallback", reason)

    def test_no_products_is_none(self) -> None:
        provider = MockLLMProvider()
        answer, info = LLMService(provider).generate_answer_with_mode("q", [])
        assert (info.mode, info.reason) == ("none", "no_products_retrieved")
        assert answer.insufficient_evidence is True and provider.call_count == 0

    def test_generate_answer_unchanged(self) -> None:
        svc = LLMService(MockLLMProvider(responses=[_VALID]))
        assert isinstance(svc.generate_answer("q", self.PRODUCTS), StructuredAnswer)
