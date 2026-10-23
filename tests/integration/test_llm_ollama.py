"""
Integration test for the real local Ollama model in Self Search (Phase 5).

Automatically skipped if the local Ollama daemon is not running on localhost:11434.
"""

from __future__ import annotations

import pytest

from src.evidence.evidence_pack import EvidenceItem, EvidencePack
from src.llm.ollama import OllamaProvider
from src.llm.schemas import StructuredAnswer
from src.llm.service import LLMService


@pytest.mark.slow
def test_real_ollama_generation() -> None:
    provider = OllamaProvider()
    if not provider.is_available():
        pytest.skip("Local Ollama daemon is not running at " + provider.base_url)

    service = LLMService(provider=provider, max_retries=1)

    products = [
        {
            "id": 6,
            "external_id": "P006",
            "title": "USB-C Fast Charging Cable 6ft",
            "brand_name": "ChargePro",
            "category_name": "Electronics",
            "price": 12.99,
            "avg_rating": 4.4,
            "description": "Heavy duty braided 100W USB-C cable.",
        }
    ]
    evidence_item = EvidenceItem(
        evidence_id="R1",
        review_id=101,
        product_id=6,
        product_external_id="P006",
        title="Charges fast",
        text="Delivers 100W charging and charges my laptop at full speed.",
        rating=5.0,
    )
    evidence_pack = EvidencePack(query="fast charging cable", items=[evidence_item], total_tokens=40, token_budget=500)

    answer = service.generate_answer(
        query="fast charging cable for laptop",
        products=products,
        evidence_pack=evidence_pack,
    )

    assert isinstance(answer, StructuredAnswer)
    assert answer.insufficient_evidence is False
    assert len(answer.recommendations) > 0
    assert answer.recommendations[0].product_id == 6
