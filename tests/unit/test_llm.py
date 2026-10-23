"""
Unit tests for the Self Search Phase 5 Local LLM Layer.
"""

from __future__ import annotations

import json
import pytest

from src.evidence.evidence_pack import EvidenceItem, EvidencePack
from src.llm.ollama import OllamaConnectionError
from src.llm.prompts import build_chat_messages
from src.llm.provider import MockLLMProvider
from src.llm.schemas import EvidenceClaim, ProductRecommendation, StructuredAnswer
from src.llm.service import LLMService, extract_json_from_text
from src.retrieval.filters import MetadataFilter


class TestSchemas:
    """Tests for Pydantic response models."""

    def test_evidence_claim(self) -> None:
        claim = EvidenceClaim(claim="100W fast charging", evidence_ids=["R1", "R2"])
        assert claim.claim == "100W fast charging"
        assert claim.evidence_ids == ["R1", "R2"]

    def test_product_recommendation(self) -> None:
        rec = ProductRecommendation(
            product_id=6,
            external_id="P006",
            title="USB-C Cable",
            why_it_matches=[EvidenceClaim(claim="Fast charging", evidence_ids=["R1"])],
            watch_outs=[EvidenceClaim(claim="A bit stiff", evidence_ids=["R2"])],
        )
        assert rec.product_id == 6
        assert len(rec.why_it_matches) == 1
        assert len(rec.watch_outs) == 1

    def test_structured_answer_valid(self) -> None:
        ans = StructuredAnswer(
            summary="Recommended cable based on fast charging speed.",
            recommendations=[
                ProductRecommendation(
                    product_id=6,
                    external_id="P006",
                    title="USB-C Cable",
                    why_it_matches=[EvidenceClaim(claim="Charges fast", evidence_ids=["R1"])],
                )
            ],
            insufficient_evidence=False,
        )
        assert ans.insufficient_evidence is False
        assert len(ans.recommendations) == 1


class TestJsonExtraction:
    """Tests for extracting JSON from raw model strings."""

    def test_plain_json(self) -> None:
        text = '{"summary": "Test", "recommendations": [], "insufficient_evidence": true}'
        extracted = extract_json_from_text(text)
        assert extracted == text

    def test_markdown_code_fence(self) -> None:
        text = 'Here is the response:\n```json\n{"summary": "Test", "recommendations": [], "insufficient_evidence": false}\n```\nHope that helps!'
        extracted = extract_json_from_text(text)
        data = json.loads(extracted)
        assert data["summary"] == "Test"
        assert data["insufficient_evidence"] is False

    def test_surrounding_commentary(self) -> None:
        text = 'Sure! {"summary": "A", "recommendations": [], "insufficient_evidence": false} Note: grounded in evidence.'
        extracted = extract_json_from_text(text)
        data = json.loads(extracted)
        assert data["summary"] == "A"


class TestPromptConstruction:
    """Tests for structured prompt building."""

    def test_build_chat_messages(self) -> None:
        products = [
            {
                "id": 6,
                "external_id": "P006",
                "title": "Fast Charging Cable",
                "brand_name": "ChargePro",
                "category_name": "Electronics",
                "price": 12.99,
                "avg_rating": 4.5,
                "description": "Durable cable.",
            }
        ]
        evidence_item = EvidenceItem(
            evidence_id="R1",
            review_id=101,
            product_id=6,
            product_external_id="P006",
            title="Great charging",
            text="Charges my phone in 30 minutes.",
            rating=5.0,
        )
        pack = EvidencePack(query="cable", items=[evidence_item], total_tokens=50, token_budget=500)
        filters = MetadataFilter(max_price=20.0, category="Electronics")

        messages = build_chat_messages(
            query="find a good charging cable",
            products=products,
            evidence_pack=pack,
            filters=filters,
        )

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert "insufficient_evidence" in messages[0]["content"]

        user_content = messages[1]["content"]
        assert "USER QUERY: find a good charging cable" in user_content
        # Budgets are INR; a product price without a currency is "unavailable", never guessed.
        assert "Max Price: ₹20" in user_content and "$" not in user_content
        assert "Price: unavailable" in user_content
        # A USD source price reaches the model only as its documented INR conversion.
        usd = build_chat_messages(query="cable", products=[{**products[0], "currency": "USD"}],
                                  evidence_pack=pack)[1]["content"]
        assert ("Price: ₹1,079 (converted from the source price at ₹83.08 per US dollar "
                "(ECB reference rate, 2023-09-29))") in usd
        assert "12.99" not in usd and "USD" not in usd and "$" not in usd

        with_currency = build_chat_messages(
            query="cable", products=products, evidence_pack=pack,
            filters=MetadataFilter(max_price=20.0, price_currency="INR", product_types=("cable",)),
        )[1]["content"]
        assert "Max Price: ₹20" in with_currency
        assert "Product type: cable" in with_currency
        assert "Fast Charging Cable" in user_content
        assert "[R1]" in user_content
        assert "Charges my phone in 30 minutes." in user_content


class TestLLMService:
    """Tests for LLMService execution, validation, retry loop, and fallback."""

    def test_empty_products_returns_insufficient_evidence(self) -> None:
        mock_provider = MockLLMProvider()
        service = LLMService(provider=mock_provider)

        answer = service.generate_answer(query="super computer", products=[])
        assert answer.insufficient_evidence is True
        assert len(answer.recommendations) == 0
        assert mock_provider.call_count == 0  # No model call needed

    def test_successful_structured_generation(self) -> None:
        valid_json = json.dumps({
            "summary": "The ChargePro cable is best for fast charging.",
            "recommendations": [
                {
                    "product_id": 6,
                    "external_id": "P006",
                    "title": "USB-C Fast Cable",
                    "why_it_matches": [{"claim": "Charges laptop in 45m", "evidence_ids": ["R1"]}],
                    "watch_outs": [],
                }
            ],
            "insufficient_evidence": False,
        })
        mock_provider = MockLLMProvider(responses=[valid_json])
        service = LLMService(provider=mock_provider)

        products = [{"id": 6, "title": "USB-C Fast Cable", "price": 12.99}]
        answer = service.generate_answer(query="fast cable", products=products)

        assert answer.insufficient_evidence is False
        assert answer.recommendations[0].product_id == 6
        assert answer.recommendations[0].why_it_matches[0].evidence_ids == ["R1"]
        assert mock_provider.call_count == 1

    def test_retry_on_malformed_json_succeeds_on_second_attempt(self) -> None:
        malformed = "I recommend product 6 because it charges fast."  # not JSON
        valid_json = json.dumps({
            "summary": "ChargePro cable is recommended.",
            "recommendations": [
                {
                    "product_id": 6,
                    "external_id": "P006",
                    "title": "Cable",
                    "why_it_matches": [{"claim": "Fast", "evidence_ids": ["R1"]}],
                    "watch_outs": [],
                }
            ],
            "insufficient_evidence": False,
        })
        mock_provider = MockLLMProvider(responses=[malformed, valid_json])
        service = LLMService(provider=mock_provider, max_retries=2)

        products = [{"id": 6, "title": "Cable", "price": 12.99}]
        answer = service.generate_answer(query="cable", products=products)

        assert answer.insufficient_evidence is False
        assert mock_provider.call_count == 2
        # Check that correction prompt was sent on retry
        second_call_messages = mock_provider.history[1]
        assert any("invalid JSON or failed schema validation" in m["content"] for m in second_call_messages)

    def test_exhausted_retries_uses_safe_fallback(self) -> None:
        # All responses malformed
        mock_provider = MockLLMProvider(responses=["bad 1", "bad 2", "bad 3"])
        service = LLMService(provider=mock_provider, max_retries=2)

        products = [{"id": 6, "title": "ChargePro Cable", "price": 12.99, "brand_name": "ChargePro"}]
        answer = service.generate_answer(query="cable", products=products)

        assert mock_provider.call_count == 3  # initial + 2 retries
        # Safe fallback generated
        assert answer.insufficient_evidence is False
        assert len(answer.recommendations) == 1
        assert answer.recommendations[0].product_id == 6
        assert "ChargePro" in answer.recommendations[0].title

    def test_provider_offline_uses_safe_fallback(self) -> None:
        class OfflineProvider(MockLLMProvider):
            def generate(self, messages, **kwargs):
                raise OllamaConnectionError("Daemon offline")

        service = LLMService(provider=OfflineProvider())
        products = [{"id": 1, "title": "Hiking Backpack", "price": 49.99}]
        answer = service.generate_answer(query="backpack", products=products)

        assert answer.insufficient_evidence is False
        assert answer.recommendations[0].product_id == 1
        assert "Hiking Backpack" in answer.recommendations[0].title
