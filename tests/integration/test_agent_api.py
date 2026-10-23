"""
Integration tests for Self Search Phase 7 FastAPI endpoints.

Tests /chat, /sessions/{id}, and /health endpoints using FastAPI TestClient
with deterministic mocked agent components.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock
import pytest

from tests.conftest import wire_generation_mode
from fastapi.testclient import TestClient

from src.agent.controller import AgentController
from src.agent.schemas import UserIntent
from src.agent.session import SessionManager
from src.api.app import app, set_agent_controller
from src.evidence.evidence_pack import EvidencePack
from src.evidence.report import SupportReport
from src.evidence.validator import EvidenceValidator
from src.llm.provider import MockLLMProvider
from src.llm.schemas import StructuredAnswer
from src.llm.service import LLMService
from src.retrieval.hybrid import HybridRetriever


@pytest.fixture
def test_controller():
    """Build a deterministic AgentController for API integration testing."""
    conn = MagicMock(spec=sqlite3.Connection)
    conn.execute.return_value.fetchone.return_value = (1,)

    retriever = MagicMock(spec=HybridRetriever)
    llm_service = MagicMock(spec=LLMService)
    validator = MagicMock(spec=EvidenceValidator)

    sample_products = [{"id": 101, "title": "Test Product", "price": 29.99}]
    sample_pack = EvidencePack(product_ids=[101], query="test")
    sample_answer = StructuredAnswer(
        summary="Here is the test product.",
        recommendations=[],
        insufficient_evidence=False,
    )
    sample_report = SupportReport(
        query="test",
        total_claims=1,
        supported_claims=1,
        unsupported_claims=0,
        fabricated_citations=0,
        cross_product_mismatches=0,
        numeric_mismatches=0,
        unverifiable_claims=0,
        support_rate=1.0,
        is_valid=True,
    )

    retriever.retrieve_with_evidence.return_value = (sample_products, sample_pack)
    llm_service.generate_answer.return_value = sample_answer
    wire_generation_mode(llm_service)
    validator.validate_answer.return_value = (sample_answer, sample_report)

    llm_provider = MockLLMProvider(default_response=json.dumps({
        "intent": "PRODUCT_SEARCH",
        "raw_query": "test query",
        "search_text": "test",
        "category": None,
        "brand": None,
        "min_price": None,
        "max_price": None,
        "min_rating": None,
        "preferences": [],
        "exclusions": [],
        "compare_product_ids": [],
        "follow_up_context": None,
        "clarification_question": None,
        "confidence": 0.9,
    }))

    session_manager = SessionManager()

    controller = AgentController(
        conn=conn,
        llm_provider=llm_provider,
        retriever=retriever,
        llm_service=llm_service,
        validator=validator,
        session_manager=session_manager,
    )
    return controller


@pytest.fixture
def client(test_controller):
    """Provide a TestClient with injected test controller."""
    set_agent_controller(test_controller)
    with TestClient(app) as test_client:
        yield test_client
    set_agent_controller(None)


class TestAgentAPI:
    """Integration test suite for the conversational API endpoints."""

    def test_health_endpoint(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["database"] == "connected"
        assert "active_sessions" in data

    def test_chat_creates_new_session(self, client: TestClient) -> None:
        payload = {"message": "wireless headphones"}
        resp = client.post("/chat", json=payload)
        assert resp.status_code == 200
        data = resp.json()

        assert "session_id" in data
        assert data["session_id"] != ""
        assert data["intent"] == "PRODUCT_SEARCH"
        assert data["answer"] is not None
        assert data["support_report"] is not None

    def test_chat_reuses_session(self, client: TestClient) -> None:
        # First turn
        resp1 = client.post("/chat", json={"message": "wireless headphones"})
        assert resp1.status_code == 200
        session_id = resp1.json()["session_id"]

        # Second turn with same session ID
        resp2 = client.post("/chat", json={"message": "under $50", "session_id": session_id})
        assert resp2.status_code == 200
        assert resp2.json()["session_id"] == session_id

    def test_get_session_lifecycle(self, client: TestClient) -> None:
        # Create session via chat
        resp = client.post("/chat", json={"message": "hello!"})
        session_id = resp.json()["session_id"]

        # Fetch session
        get_resp = client.get(f"/sessions/{session_id}")
        assert get_resp.status_code == 200
        session_data = get_resp.json()
        assert session_data["session_id"] == session_id
        assert session_data["conversation_turn_count"] >= 2

    def test_get_nonexistent_session_returns_404(self, client: TestClient) -> None:
        resp = client.get("/sessions/does-not-exist")
        assert resp.status_code == 404

    def test_delete_session_lifecycle(self, client: TestClient) -> None:
        resp = client.post("/chat", json={"message": "hello!"})
        session_id = resp.json()["session_id"]

        # Delete session
        del_resp = client.delete(f"/sessions/{session_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["status"] == "deleted"

        # Verify it's gone
        get_resp = client.get(f"/sessions/{session_id}")
        assert get_resp.status_code == 404

    def test_delete_nonexistent_session_returns_404(self, client: TestClient) -> None:
        resp = client.delete("/sessions/does-not-exist")
        assert resp.status_code == 404

    def test_chat_validation_error_on_empty_message(self, client: TestClient) -> None:
        resp = client.post("/chat", json={"message": ""})
        assert resp.status_code == 422  # Pydantic validation error for min_length=1

    def test_api_key_auth(self, monkeypatch: pytest.MonkeyPatch, test_controller) -> None:
        monkeypatch.setenv("API_KEY", "secret-test-key")
        set_agent_controller(test_controller)

        # Reload verify_api_key behavior with monkeypatched environment
        from src.api import app as api_module
        api_module._API_KEY = "secret-test-key"

        try:
            with TestClient(app) as auth_client:
                # Without key -> 401
                resp = auth_client.post("/chat", json={"message": "hello"})
                assert resp.status_code == 401

                # With wrong key -> 401
                resp = auth_client.post(
                    "/chat",
                    json={"message": "hello"},
                    headers={"X-API-Key": "wrong-key"},
                )
                assert resp.status_code == 401

                # With correct key -> 200
                resp = auth_client.post(
                    "/chat",
                    json={"message": "hello"},
                    headers={"X-API-Key": "secret-test-key"},
                )
                assert resp.status_code == 200
        finally:
            api_module._API_KEY = None
            set_agent_controller(None)


def test_default_controller_wiring(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The production factory must build a controller whose LLMService uses the Ollama provider."""
    from types import SimpleNamespace

    from src.api import app as api_module

    provider = MockLLMProvider()
    monkeypatch.setattr(api_module, "get_settings", lambda: SimpleNamespace(
        db_path=str(tmp_path / "t.db"),
    ))
    monkeypatch.setattr(api_module, "OllamaProvider", lambda: provider)
    monkeypatch.setattr(api_module, "HybridRetriever", lambda conn: MagicMock(spec=HybridRetriever))
    checked = []  # the index-manifest startup check has its own tests (test_api_index_check.py)
    monkeypatch.setattr(api_module, "require_valid_manifest", lambda conn, index_dir: checked.append(index_dir))

    controller = api_module.create_default_agent_controller()

    assert checked, "the production factory must run the index-manifest check"
    assert controller.llm_provider is provider
    assert controller.llm_service.provider is provider
    assert isinstance(controller.validator, EvidenceValidator)
    controller.conn.close()


def test_api_connection_usable_from_worker_threads(tmp_path) -> None:
    """FastAPI runs sync endpoints in a threadpool; the shared connection must allow that."""
    from concurrent.futures import ThreadPoolExecutor

    from src.api.app import open_api_connection

    conn = open_api_connection(tmp_path / "threads.db")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: conn.execute("SELECT 1").fetchone()[0], range(4)))
        assert results == [1, 1, 1, 1]
    finally:
        conn.close()


def _row(pid: int) -> tuple:
    return (pid, f"EXT{pid}", f"Product {pid}", "desc", float(pid), 4.0, 10, None, "{}", "Brand", "Cat")


def test_offline_multi_turn_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Search -> refine -> question -> compare over /chat with Ollama unavailable.

    Uses the real QueryParser (rule-based fallback), IntentRouter and SessionState;
    only retrieval, generation and validation are stubbed. Every answer-generating
    turn must pass through EvidenceValidator.
    """
    monkeypatch.setattr(
        "src.agent.intent_router.assemble_evidence_pack",
        lambda conn, product_ids, **kw: EvidencePack(product_ids=list(product_ids), query=""),
    )
    conn = MagicMock(spec=sqlite3.Connection)
    cursor = conn.cursor.return_value
    cursor.execute.return_value.fetchone.return_value = _row(102)
    cursor.execute.return_value.fetchall.return_value = [_row(101), _row(102)]

    retriever = MagicMock(spec=HybridRetriever)
    retriever.retrieve_with_evidence.return_value = (
        [{"id": 101}, {"id": 102}, {"id": 103}],
        EvidencePack(product_ids=[101, 102, 103], query=""),
    )
    answer = StructuredAnswer(summary="ok", recommendations=[], insufficient_evidence=False)
    report = SupportReport(
        query="", total_claims=0, supported_claims=0, unsupported_claims=0,
        fabricated_citations=0, cross_product_mismatches=0, numeric_mismatches=0,
        unverifiable_claims=0, support_rate=1.0, is_valid=True,
    )
    llm_service = MagicMock(spec=LLMService)
    llm_service.generate_answer.return_value = answer
    wire_generation_mode(llm_service)
    validator = MagicMock(spec=EvidenceValidator)
    validator.validate_answer.return_value = (answer, report)

    controller = AgentController(
        conn=conn,
        llm_provider=MockLLMProvider(available=False),
        retriever=retriever,
        llm_service=llm_service,
        validator=validator,
    )
    set_agent_controller(controller)
    try:
        with TestClient(app) as client:
            r1 = client.post("/chat", json={"message": "wireless headphones under ₹8000"}).json()
            sid = r1["session_id"]
            assert r1["intent"] == "PRODUCT_SEARCH"
            assert r1["active_filters"]["max_price"] == 8000.0

            r2 = client.post("/chat", json={"message": "only 4+ stars", "session_id": sid}).json()
            assert r2["intent"] == "REFINE_SEARCH"
            assert r2["active_filters"]["max_price"] == 8000.0
            assert r2["active_filters"]["min_rating"] == 4.0
            refined_query = retriever.retrieve_with_evidence.call_args.kwargs["query"]
            assert refined_query.startswith("wireless headphones")
            assert "8000" not in refined_query  # the budget is a filter, not search text
            assert retriever.retrieve_with_evidence.call_args.kwargs["filters"].max_price == 8000.0

            r3 = client.post("/chat", json={"message": "is it waterproof?", "session_id": sid}).json()
            assert r3["intent"] == "PRODUCT_QUESTION"
            assert r3["answer"] is None
            assert "Which one" in r3["clarification_question"]

            r4 = client.post(
                "/chat", json={"message": "is the second one waterproof?", "session_id": sid}
            ).json()
            assert r4["intent"] == "PRODUCT_QUESTION"
            assert r4["support_report"] is not None
            assert llm_service.generate_answer.call_args.kwargs["products"][0]["id"] == 102

            r5 = client.post("/chat", json={"message": "compare these two", "session_id": sid}).json()
            assert r5["intent"] == "COMPARE"
            assert r5["support_report"] is not None

            r6 = client.post("/chat", json={"message": "tell me a joke", "session_id": sid}).json()
            assert r6["intent"] == "OUT_OF_SCOPE"
            assert r6["answer"] is None

            r7 = client.post("/chat", json={"message": "I want something nice", "session_id": sid}).json()
            assert r7["intent"] == "CLARIFY"
            assert r7["answer"] is None

        # Search and refine are product-first (no model call); the positional question and
        # the comparison call the model. Every answer, including the claim-free search ones,
        # went through the validator.
        assert llm_service.generate_answer.call_count == 2
        assert validator.validate_answer.call_count == 4
    finally:
        set_agent_controller(None)
