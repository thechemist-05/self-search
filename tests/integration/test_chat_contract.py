"""
Integration tests for the Phase 9 /chat contract and new endpoints.

Uses a temporary database with real evidence-pack assembly, the real
EvidenceValidator (wrapped in a spy to capture what it validated) and the real
LLMService driven by a scripted MockLLMProvider. Only retrieval ranking is
stubbed. No network or Ollama.

Searches are product-first (no model call); model answers about a search's
results come from POST /chat/summary, so the answer contract is tested there.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.agent.controller import AgentController
from src.api.app import app, set_agent_controller
from src.evidence.evidence_pack import assemble_evidence_pack
from src.evidence.validator import EvidenceValidator
from src.llm.ollama import OllamaConnectionError
from src.llm.provider import MockLLMProvider
from src.llm.service import LLMService
from src.retrieval.hybrid import HybridRetriever
from tests.conftest import personal_catalog_products

PHASE8_KEYS = {
    "session_id", "intent", "answer", "clarification_question", "smalltalk_response",
    "support_report", "active_filters", "shown_product_ids", "parsing_confidence",
}
PHASE9_KEYS = {"result_product_ids", "products", "evidence", "generation", "constraints"}
PROFILE_KEYS = {"personalization", "preference_suggestions"}  # removed with user profiles (2026-10-21)

# The model "recommends" product 3 with a fabricated title/price and an unknown product 999.
LLM_ANSWER = json.dumps({
    "summary": "The Hiking Backpack fits best.",
    "recommendations": [
        {"product_id": 3, "title": "FAKE TITLE costs $1", "why_it_matches": [
            {"claim": "Reviewers say it is very light and waterproof", "evidence_ids": ["R1"]},
            {"claim": "It costs only $1", "evidence_ids": ["R99"]},
        ]},
        {"product_id": 999, "title": "Invented Product", "why_it_matches": []},
    ],
    "insufficient_evidence": False,
})


def _add_reviews(conn: sqlite3.Connection) -> None:
    rows = []
    for pid in range(1, 7):
        rows.append((pid, f"R{pid}a", 5, "Love it", f"Product {pid} is very light and waterproof, great value", 7))
        rows.append((pid, f"R{pid}b", 2, "Meh", f"Product {pid} broke after a month", 1))
    conn.executemany(
        "INSERT INTO reviews (product_id, source_id, external_id, rating, title, text, helpful_votes, verified_flag) "
        "VALUES (?, 1, ?, ?, ?, ?, ?, 1)",
        rows,
    )
    conn.commit()


@pytest.fixture
def env(personal_db: sqlite3.Connection):
    _add_reviews(personal_db)
    retriever = MagicMock(spec=HybridRetriever)

    def retrieve_with_evidence(**kw):
        products = personal_catalog_products()[: kw["limit"]]
        pack = assemble_evidence_pack(personal_db, [p["id"] for p in products], query=kw["query"],
                                      max_reviews_per_product=kw["max_reviews_per_product"],
                                      token_budget=kw["token_budget"])
        return products, pack

    retriever.retrieve_with_evidence.side_effect = retrieve_with_evidence
    retriever.retrieve.side_effect = lambda **kw: personal_catalog_products()[: kw["limit"]]

    provider = MockLLMProvider(default_response=LLM_ANSWER)
    validator = MagicMock(spec=EvidenceValidator, wraps=EvidenceValidator(conn=personal_db))
    controller = AgentController(
        conn=personal_db,
        llm_provider=MockLLMProvider(available=False),  # query parsing uses the offline rules
        retriever=retriever,
        llm_service=LLMService(provider),
        validator=validator,
    )
    controller.intent_router.search_limit = 4
    set_agent_controller(controller)
    with TestClient(app) as client:
        yield {"client": client, "controller": controller, "validator": validator, "provider": provider,
               "db": personal_db}
    set_agent_controller(None)


def _search(c: TestClient, message: str = "outdoor gear", **extra) -> dict:
    resp = c.post("/chat", json={"message": message, **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _summary(c: TestClient, search: dict) -> dict:
    """POST /chat/summary for the results of ``search``."""
    resp = c.post("/chat/summary", json={"session_id": search["session_id"],
                                         "result_product_ids": search["result_product_ids"]})
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestCompatibility:
    def test_legacy_request_still_works_and_old_fields_keep_meaning(self, env) -> None:
        data = _search(env["client"])
        assert set(data) == PHASE8_KEYS | PHASE9_KEYS
        assert data["intent"] == "PRODUCT_SEARCH"
        assert data["answer"]["summary"] == "" and data["answer"]["recommendations"] == []  # product-first
        assert data["support_report"]["total_claims"] == 0
        assert data["shown_product_ids"] == [1, 2, 3, 4]
        assert not PROFILE_KEYS & set(data)

        summary = _summary(env["client"], data)
        assert set(summary) == PHASE8_KEYS | PHASE9_KEYS
        assert summary["intent"] == "PRODUCT_SEARCH"
        # The answer is otherwise untouched, but a dollar amount the model wrote is never shown.
        assert summary["answer"]["recommendations"][0]["title"] == "FAKE TITLE costs [price in ₹ shown on the card]"
        assert "$" not in json.dumps(summary, ensure_ascii=False)
        assert summary["support_report"]["total_claims"] == 2
        assert not PROFILE_KEYS & set(summary)

    def test_non_answer_turn_has_safe_defaults(self, env) -> None:
        data = _search(env["client"], "hello")
        assert data["intent"] == "SMALLTALK"
        assert (data["result_product_ids"], data["products"], data["evidence"]) == ([], [], [])
        assert data["generation"] is None


class TestDatabaseBackedCards:
    def test_cards_use_database_values_never_llm_values(self, env) -> None:
        search = _search(env["client"])
        assert search["result_product_ids"] == [1, 2, 3, 4]   # retrieval order
        assert not any(p["recommended"] for p in search["products"])
        data = _summary(env["client"], search)
        assert data["result_product_ids"] == [1, 2, 3, 4]     # the summary never reorders the cards
        cards = {p["id"]: p for p in data["products"]}
        assert set(cards) == {1, 2, 3, 4}
        # USD 49.99 in the database; cards carry only the documented INR conversion.
        assert cards[3]["title"] == "Hiking Backpack" and (cards[3]["price"], cards[3]["currency"]) == (4153.0, "INR")
        assert cards[3]["price_status"] == "converted" and cards[3]["price_conversion"]["rate"] == 83.0815
        assert cards[3]["recommended"] is True and cards[1]["recommended"] is False
        assert cards[3]["retrieval_rank"] == 3
        assert cards[3]["local_review_count"] == 2
        assert cards[3]["aspects"] == []
        assert "FAKE" not in json.dumps(data["products"]) and "Invented" not in json.dumps(data["products"])


class TestEvidenceAlignment:
    def test_evidence_is_exactly_the_validated_pack(self, env) -> None:
        data = _summary(env["client"], _search(env["client"]))
        validated_pack = env["validator"].validate_answer.call_args.args[1]
        assert [(e["evidence_id"], e["review_id"], e["product_id"], e["text"]) for e in data["evidence"]] == [
            (i.evidence_id, i.review_id, i.product_id, i.text) for i in validated_pack.items
        ]
        assert "R99" not in {e["evidence_id"] for e in data["evidence"]}
        statuses = {r["claim_text"]: r["status"] for r in data["support_report"]["claim_results"]}
        assert statuses["It costs only [price in ₹ shown on the card]"] == "FABRICATED_CITATION"

    def test_question_path_evidence_alignment(self, env) -> None:
        sid = _search(env["client"])["session_id"]
        data = _search(env["client"], "is it durable?", session_id=sid, product_ids=[2])
        validated_pack = env["validator"].validate_answer.call_args.args[1]
        assert data["intent"] == "PRODUCT_QUESTION"
        assert data["result_product_ids"] == [2]
        assert {e["product_id"] for e in data["evidence"]} == {2}
        assert [e["evidence_id"] for e in data["evidence"]] == [i.evidence_id for i in validated_pack.items]


class TestGenerationMode:
    def test_search_is_not_generated_and_summary_is_llm(self, env) -> None:
        search = _search(env["client"])
        assert search["generation"] == {"mode": "none", "reason": "summary_not_requested", "model": None,
                                        "attempts": None}
        assert _summary(env["client"], search)["generation"]["mode"] == "llm"

    def test_fallback_after_model_error_is_reported_and_still_validated(self, env) -> None:
        def down(_messages):
            raise OllamaConnectionError("connection refused")

        search = _search(env["client"])
        env["provider"].handler = down
        calls_before = env["validator"].validate_answer.call_count
        data = _summary(env["client"], search)
        assert data["generation"] == {"mode": "fallback", "reason": "provider_unavailable", "model": None, "attempts": None}
        assert env["validator"].validate_answer.call_count == calls_before + 1
        assert data["support_report"] is not None


class TestProductSelection:
    def test_compare_selected_products(self, env) -> None:
        sid = _search(env["client"])["session_id"]
        data = _search(env["client"], "compare these", session_id=sid, product_ids=[4, 1])
        assert data["intent"] == "COMPARE"
        assert set(data["result_product_ids"]) == {1, 4}
        assert data["support_report"] is not None

    @pytest.mark.parametrize("ids", [[], [5], [1, 1], [1, 2, 3, 4, 2], [999]])
    def test_invalid_selection_rejected_without_state_change(self, env, ids) -> None:
        c = env["client"]
        sid = _search(c)["session_id"]  # shows products 1-4
        turns = c.get(f"/sessions/{sid}").json()["conversation_turn_count"]
        resp = c.post("/chat", json={"message": "compare", "session_id": sid, "product_ids": ids})
        assert resp.status_code == 422
        assert c.get(f"/sessions/{sid}").json()["conversation_turn_count"] == turns

    def test_selection_needs_existing_session(self, env) -> None:
        resp = env["client"].post("/chat", json={"message": "is it good?", "product_ids": [1]})
        assert resp.status_code == 422
        assert env["controller"].session_manager.count == 0

    def test_smalltalk_with_selection_stays_smalltalk(self, env) -> None:
        sid = _search(env["client"])["session_id"]
        data = _search(env["client"], "thanks", session_id=sid, product_ids=[1])
        assert data["intent"] == "SMALLTALK" and data["answer"] is None


class TestNoProfiles:
    @pytest.mark.parametrize("method,path", [
        ("post", "/users"), ("get", "/users/1"), ("delete", "/users/1"), ("get", "/users/1/export"),
        ("put", "/users/1/consent"), ("get", "/users/1/preferences"), ("put", "/users/1/preferences/budget_max"),
        ("get", "/users/1/wishlist"), ("post", "/users/1/wishlist"), ("put", "/users/1/feedback/1"),
    ])
    def test_profile_endpoints_are_gone(self, env, method, path) -> None:
        assert getattr(env["client"], method)(path).status_code == 404

    def test_old_clients_sending_user_id_still_get_an_answer(self, env) -> None:
        data = _search(env["client"], "outdoor gear", user_id=1)
        assert data["intent"] == "PRODUCT_SEARCH" and data["products"]
        assert not PROFILE_KEYS & set(data)

    def test_saved_items_phrasing_is_an_ordinary_search(self, env) -> None:
        data = _search(env["client"], "show me something like what I saved")
        assert data["intent"] in {"PRODUCT_SEARCH", "REFINE_SEARCH", "CLARIFY"}  # no PERSONAL intent any more
        assert not PROFILE_KEYS & set(data)


class TestNewEndpoints:
    def test_product_detail(self, env) -> None:
        c = env["client"]
        detail = c.get("/products/3").json()
        assert detail["product"]["title"] == "Hiking Backpack"
        assert [r["helpful_votes"] for r in detail["reviews"]] == [7, 1]
        assert c.get("/products/999").status_code == 404

    def test_clear_filter(self, env) -> None:
        c = env["client"]
        data = _search(c, "bags under ₹6000 4+ stars")
        sid = data["session_id"]
        assert data["active_filters"]["max_price"] == 6000.0

        cleared = c.delete(f"/sessions/{sid}/filters/max_price").json()
        assert cleared["active_filters"]["max_price"] is None
        assert cleared["active_filters"]["min_rating"] == 4.0
        assert c.get(f"/sessions/{sid}").json()["active_filters"]["max_price"] is None
        assert c.delete(f"/sessions/{sid}/filters/colour").status_code == 422
        assert c.delete("/sessions/nope/filters/max_price").status_code == 404


def _evidence_ids_by_product(messages: list[dict]) -> dict[int, list[str]]:
    """Evidence IDs per product, read from the prompt the model received."""
    out: dict[int, list[str]] = {}
    for eid, pid in re.findall(r"\[(R\d+)\] \(Product ID: (\d+)", messages[-1]["content"]):
        out.setdefault(int(pid), []).append(eid)
    return out


class TestOptionalSummary:
    """Product-first search, and the model summary only on request (POST /chat/summary)."""

    def test_search_never_calls_the_model(self, env) -> None:
        data = _search(env["client"], "outdoor gear under ₹6000 4+ stars")
        assert env["provider"].call_count == 0
        assert data["products"] and data["support_report"]["is_valid"]

    def test_summary_calls_the_model_once_and_covers_the_top_three(self, env) -> None:
        search = _search(env["client"])
        summary = _summary(env["client"], search)
        assert env["provider"].call_count == 1
        prompt = env["provider"].history[0][-1]["content"]
        assert re.findall(r"- Product ID: (\d+) \|", prompt) == ["1", "2", "3"]   # at most three products
        assert {e["product_id"] for e in summary["evidence"]} <= {1, 2, 3}

    def test_repeated_requests_reuse_the_summary(self, env) -> None:
        search = _search(env["client"])
        first = _summary(env["client"], search)
        again = _summary(env["client"], search)
        assert env["provider"].call_count == 1
        assert again == first

    def test_concurrent_duplicate_requests_call_the_model_once(self, env) -> None:
        search = _search(env["client"])

        def slow(_messages):
            time.sleep(0.3)
            return LLM_ANSWER

        env["provider"].handler = slow
        results: list[int] = []

        def ask() -> None:
            r = env["client"].post("/chat/summary", json={"session_id": search["session_id"]})
            results.append(r.status_code)

        threads = [threading.Thread(target=ask) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results == [200, 200, 200]
        assert env["provider"].call_count == 1

    def test_failed_summary_keeps_the_results_and_can_be_retried(self, env) -> None:
        search = _search(env["client"])

        def down(_messages):
            raise OllamaConnectionError("connection refused")

        env["provider"].handler = down
        failed = _summary(env["client"], search)
        assert failed["generation"]["mode"] == "fallback"
        assert failed["result_product_ids"] == search["result_product_ids"]  # same products, same order
        assert [p["id"] for p in failed["products"]] == [p["id"] for p in search["products"]]

        env["provider"].handler = None  # the model is back: a retry is not served from a cache
        retried = _summary(env["client"], search)
        assert retried["generation"]["mode"] == "llm"
        # The session's results are untouched, so follow-ups still work.
        compare = _search(env["client"], "compare these", session_id=search["session_id"], product_ids=[1, 2])
        assert compare["intent"] == "COMPARE" and compare["support_report"] is not None

    def test_cross_product_and_numeric_claims_are_rejected(self, env) -> None:
        def answer(messages):
            ids = _evidence_ids_by_product(messages)
            return json.dumps({
                "summary": "Picks.",
                "recommendations": [{"product_id": 3, "why_it_matches": [
                    {"claim": "Very light and waterproof", "evidence_ids": [ids[1][0]]},   # a review of product 1
                    {"claim": "It weighs 12 kg and costs $3", "evidence_ids": [ids[3][0]]},
                ]}],
                "insufficient_evidence": False,
            })

        env["provider"].handler = answer
        data = _summary(env["client"], _search(env["client"]))
        statuses = [r["status"] for r in data["support_report"]["claim_results"]]
        assert statuses[0] == "CROSS_PRODUCT_MISMATCH"
        assert statuses[1] in {"NUMERIC_MISMATCH", "UNSUPPORTED_LOW_SIMILARITY"}
        assert statuses[1] != "SUPPORTED"
        assert data["support_report"]["is_valid"] is False

    def test_summary_requires_current_results(self, env) -> None:
        c = env["client"]
        assert c.post("/chat/summary", json={"session_id": "nope"}).status_code == 404
        sid = _search(c, "hello")["session_id"]               # a session without a search
        assert c.post("/chat/summary", json={"session_id": sid}).status_code == 409
        first = _search(c, "outdoor gear", session_id=sid)
        _search(c, "bags under ₹6000", session_id=sid)        # newer results replace them
        stale = c.post("/chat/summary", json={"session_id": sid, "result_product_ids": [9, 8]})
        assert stale.status_code == 409
        assert env["provider"].call_count == 0
        assert first["products"]

    def test_summary_requires_the_api_key_when_set(self, env, monkeypatch) -> None:
        monkeypatch.setattr("src.api.app._API_KEY", "secret")
        search = env["client"].post("/chat", json={"message": "outdoor gear"}, headers={"X-API-Key": "secret"}).json()
        r = env["client"].post("/chat/summary", json={"session_id": search["session_id"]})
        assert r.status_code == 401
        ok = env["client"].post("/chat/summary", json={"session_id": search["session_id"]},
                                headers={"X-API-Key": "secret"})
        assert ok.status_code == 200
