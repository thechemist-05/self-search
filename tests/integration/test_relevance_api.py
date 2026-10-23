"""
End-to-end regression tests for the observed relevance bug, through /chat.

Real FastAPI app, real HybridRetriever (FTS keyword + offline hashed vectors),
real evidence packs and EvidenceValidator (wrapped in a spy), and LLMService
with the local model unavailable (offline fallback), on a copy of the real
20-product catalogue (all prices USD). No network, no Ollama.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.agent.controller import AgentController
from src.api.app import app, open_api_connection, set_agent_controller
from src.evidence.validator import EvidenceValidator
from src.llm.ollama import OllamaConnectionError
from src.llm.provider import MockLLMProvider
from src.llm.service import LLMService
from src.retrieval.hybrid import HybridRetriever

REAL_DB = Path("data/processed/shelfsense.db")
SHOE_QUERY = "I need a shoe under 4000 rupees, which can be everyday use, sneakers."
ELECTRONICS = {2, 4, 6, 8, 10, 12, 14, 16, 18, 20}


def _offline(_messages):
    raise OllamaConnectionError("Ollama is not running")


@pytest.fixture
def env(tmp_path: Path):
    if not REAL_DB.exists():
        pytest.skip("data/processed/shelfsense.db not found")
    shutil.copy(REAL_DB, tmp_path / "shelfsense.db")
    conn = open_api_connection(tmp_path / "shelfsense.db")
    provider = MockLLMProvider(handler=_offline)
    validator = MagicMock(spec=EvidenceValidator, wraps=EvidenceValidator(conn=conn))
    controller = AgentController(
        conn=conn, llm_provider=MockLLMProvider(available=False), retriever=HybridRetriever(conn=conn),
        llm_service=LLMService(provider), validator=validator,
    )
    set_agent_controller(controller)
    with TestClient(app) as client:
        yield {"client": client, "provider": provider, "validator": validator}
    set_agent_controller(None)
    conn.close()


def _chat(env, message: str, **extra) -> dict:
    resp = env["client"].post("/chat", json={"message": message, **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _price_item(data: dict) -> dict:
    return next(i for i in data["constraints"]["items"] if i["kind"] == "price")


class TestObservedShoeQuery:
    """2026-10-23: budgets are INR, and the catalogue's USD prices are compared on their documented
    INR conversion. The only shoe (id 3) is USD 89.99 = ₹7,477."""

    def test_rupee_budget_is_applied_to_inr_prices(self, env) -> None:
        data = _chat(env, SHOE_QUERY)  # under 4000 rupees
        assert data["clarification_question"] is None
        assert data["products"] == [] and data["result_product_ids"] == []  # ₹7,477 is over budget, no padding
        price = _price_item(data)
        assert price["status"] == "applied" and price["value"]["currency"] == "INR"
        assert data["constraints"]["matching_products"] == 0
        assert data["constraints"]["catalog_currencies"] == ["INR"]
        assert env["provider"].call_count == 0

    def test_a_large_enough_rupee_budget_returns_only_footwear(self, env) -> None:
        data = _chat(env, "I need a shoe under 8000 rupees, which can be everyday use, sneakers.")
        assert data["result_product_ids"] == [3]
        card = data["products"][0]
        assert card["title"] == "Marathon Running Shoes - Men"
        assert (card["price"], card["currency"], card["price_status"]) == (7477.0, "INR", "converted")
        kinds = {c["kind"] for c in card["constraint_checks"]}
        assert {"product_type", "price"} <= kinds
        assert {e["product_id"] for e in data["evidence"]} <= {3}  # evidence only for allowed products
        env["validator"].validate_answer.assert_called_once()
        assert data["generation"]["mode"] == "none"  # product-first: no model call for a search

    def test_dollar_budget_is_clarified_not_converted(self, env) -> None:
        data = _chat(env, "I need a shoe under $100, which can be everyday use, sneakers.")
        assert data["answer"] is None and data["products"] == []
        assert "rupees (₹)" in data["clarification_question"] and "$" not in data["clarification_question"]
        assert _price_item(data)["status"] == "not_applied"
        env["validator"].validate_answer.assert_not_called()

    def test_no_electronics_and_nothing_over_budget(self, env) -> None:
        for q in ("sneakers under ₹8000", "shoes for everyday use", "footwear under ₹7500"):
            data = _chat(env, q)
            ids = set(data["result_product_ids"])
            assert not ids & ELECTRONICS, (q, ids)
            assert all(p["currency"] == "INR" and p["price"] <= 8000 for p in data["products"])


class TestInsufficientCoverage:
    def test_no_match_is_reported_without_padding(self, env) -> None:
        data = _chat(env, "sneakers under ₹4000")
        assert data["products"] == [] and data["result_product_ids"] == []
        assert data["answer"]["insufficient_evidence"] is True
        assert data["constraints"]["matching_products"] == 0
        env["validator"].validate_answer.assert_called_once()  # the no-match answer is still validated

    def test_unknown_product_returns_nothing_rather_than_noise(self, env) -> None:
        data = _chat(env, "coffee maker")
        assert data["products"] == [] and data["answer"]["insufficient_evidence"] is True


class TestConstraintCombinations:
    def test_unstated_currency_is_read_as_rupees_and_shown(self, env) -> None:
        data = _chat(env, "shoe under 8000")
        assert data["result_product_ids"] == [3]
        price = _price_item(data)
        assert price["status"] == "applied" and price["value"]["currency"] == "INR"
        assert price["value"]["currency_source"] == "catalog_default"
        assert "all prices are in ₹" in price["label"]

    def test_type_only_returns_that_type(self, env) -> None:
        assert _chat(env, "wireless earbuds")["result_product_ids"] == [2]
        assert _chat(env, "bluetooth speaker")["result_product_ids"] == [8]

    def test_brand_and_term_exclusions(self, env) -> None:
        assert 2 not in _chat(env, "wireless gadgets not SoundWave")["result_product_ids"]
        data = _chat(env, "mechanical keyboard without RGB")
        assert data["result_product_ids"] == []  # the only keyboard mentions RGB
        assert any(i["kind"] == "excluded_term" for i in data["constraints"]["items"])

    def test_type_price_and_rating_together(self, env) -> None:
        assert _chat(env, "backpack under ₹5000 4+ stars")["result_product_ids"] == [1]  # ₹4,153
        assert _chat(env, "backpack under ₹4000")["result_product_ids"] == []

    def test_refinement_keeps_the_product_type(self, env) -> None:
        sid = _chat(env, "shoes")["session_id"]
        data = _chat(env, "only 4+ stars", session_id=sid)
        assert data["intent"] == "REFINE_SEARCH" and data["result_product_ids"] == [3]
        kinds = {i["kind"] for i in data["constraints"]["items"]}
        assert {"product_type", "min_rating"} <= kinds

    def test_question_and_compare_turns_have_no_constraint_report(self, env) -> None:
        sid = _chat(env, "wireless gadgets")["session_id"]
        data = _chat(env, "compare the top 2", session_id=sid)
        assert data["intent"] == "COMPARE" and data["constraints"] is None
        assert all(p["constraint_checks"] == [] for p in data["products"])
