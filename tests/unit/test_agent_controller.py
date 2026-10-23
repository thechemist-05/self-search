"""
Unit tests for Self Search Phase 7 Agent Controller.

Tests the full orchestrator coordinating session state, query parsing,
intent routing, evidence validation, and conversation history recording.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock
import pytest

from tests.conftest import wire_generation_mode

from src.agent.controller import AgentController
from src.agent.query_parser import QueryParser
from src.agent.schemas import AgentResponse, ParsedQuery, UserIntent
from src.agent.session import SessionManager
from src.evidence.evidence_pack import EvidencePack
from src.evidence.report import SupportReport
from src.evidence.validator import EvidenceValidator
from src.llm.provider import MockLLMProvider
from src.llm.schemas import StructuredAnswer
from src.llm.service import LLMService
from src.retrieval.hybrid import HybridRetriever


@pytest.fixture
def agent_components():
    """Setup mock agent components."""
    conn = MagicMock(spec=sqlite3.Connection)
    retriever = MagicMock(spec=HybridRetriever)
    llm_service = MagicMock(spec=LLMService)
    validator = MagicMock(spec=EvidenceValidator)

    sample_products = [{"id": 10, "title": "Wireless Earbuds", "price": 49.99}]
    sample_pack = EvidencePack(product_ids=[10], query="earbuds")
    sample_answer = StructuredAnswer(
        summary="Recommended Wireless Earbuds for great sound.",
        recommendations=[],
        insufficient_evidence=False,
    )
    sample_report = SupportReport(
        query="earbuds",
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

    return {
        "conn": conn,
        "retriever": retriever,
        "llm_service": llm_service,
        "validator": validator,
        "sample_products": sample_products,
        "sample_answer": sample_answer,
        "sample_report": sample_report,
    }


class TestAgentController:
    """Tests for AgentController orchestration."""

    def test_first_message_creates_session(self, agent_components) -> None:
        llm_parse_response = json.dumps({
            "intent": "PRODUCT_SEARCH",
            "raw_query": "earbuds under ₹5000",
            "search_text": "earbuds",
            "category": "earbuds",
            "brand": None,
            "min_price": None,
            "max_price": 5000.0,
            "min_rating": None,
            "preferences": [],
            "exclusions": [],
            "compare_product_ids": [],
            "follow_up_context": None,
            "clarification_question": None,
            "confidence": 0.95,
        })
        provider = MockLLMProvider(responses=[llm_parse_response])
        session_manager = SessionManager()

        controller = AgentController(
            conn=agent_components["conn"],
            llm_provider=provider,
            retriever=agent_components["retriever"],
            llm_service=agent_components["llm_service"],
            validator=agent_components["validator"],
            session_manager=session_manager,
        )

        resp = controller.handle_message(user_message="earbuds under ₹5000")

        assert resp.session_id is not None
        assert session_manager.get(resp.session_id) is not None
        assert resp.intent == UserIntent.PRODUCT_SEARCH
        assert resp.answer is not None
        assert resp.support_report is not None
        assert resp.shown_product_ids == [10]
        assert resp.active_filters["max_price"] == 5000.0

    def test_reuse_existing_session_id(self, agent_components) -> None:
        session_manager = SessionManager()
        s = session_manager.get_or_create("sess-user-1")

        llm_response = json.dumps({
            "intent": "SMALLTALK",
            "raw_query": "hi",
            "search_text": "",
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
            "confidence": 1.0,
        })
        provider = MockLLMProvider(responses=[llm_response])

        controller = AgentController(
            conn=agent_components["conn"],
            llm_provider=provider,
            retriever=agent_components["retriever"],
            llm_service=agent_components["llm_service"],
            validator=agent_components["validator"],
            session_manager=session_manager,
        )

        resp = controller.handle_message(user_message="hi", session_id="sess-user-1")
        assert resp.session_id == "sess-user-1"
        assert resp.intent == UserIntent.SMALLTALK
        assert resp.smalltalk_response is not None

        # Check turn recording
        assert len(s.conversation_history) == 2  # user turn + assistant turn
        assert s.conversation_history[0]["role"] == "user"
        assert s.conversation_history[1]["role"] == "assistant"

    def test_clarify_flow_end_to_end(self, agent_components) -> None:
        provider = MockLLMProvider()
        controller = AgentController(
            conn=agent_components["conn"],
            llm_provider=provider,
            retriever=agent_components["retriever"],
            llm_service=agent_components["llm_service"],
            validator=agent_components["validator"],
        )

        resp = controller.handle_message(user_message="")
        assert resp.intent == UserIntent.CLARIFY
        assert resp.clarification_question is not None
        assert resp.answer is None
        agent_components["retriever"].retrieve_with_evidence.assert_not_called()

    def test_error_handling_graceful_degradation(self, agent_components) -> None:
        provider = MockLLMProvider()
        router_mock = MagicMock()
        router_mock.route.side_effect = RuntimeError("Database connection lost")

        controller = AgentController(
            conn=agent_components["conn"],
            llm_provider=provider,
            retriever=agent_components["retriever"],
            llm_service=agent_components["llm_service"],
            validator=agent_components["validator"],
            intent_router=router_mock,
        )

        resp = controller.handle_message("show me headphones")
        assert resp.clarification_question is not None
        assert "issue" in resp.clarification_question.lower() or "rephrase" in resp.clarification_question.lower()
