"""
Unit tests for Self Search Phase 7 Intent Router.

Tests dispatching for all 7 user intents, verification that the evidence
validation pipeline is enforced for answer-generating intents, and that
canned responses are returned for conversational and clarifying intents.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock
import pytest

from tests.conftest import wire_generation_mode

from src.agent.intent_router import IntentRouter, SMALLTALK_RESPONSES, OUT_OF_SCOPE_RESPONSE
from src.agent.schemas import ParsedQuery, UserIntent
from src.agent.session import SessionState
from src.evidence.evidence_pack import EvidencePack
from src.evidence.report import SupportReport
from src.evidence.validator import EvidenceValidator
from src.llm.schemas import StructuredAnswer
from src.llm.service import LLMService
from src.retrieval.filters import MetadataFilter
from src.retrieval.hybrid import HybridRetriever


@pytest.fixture
def mock_pipeline():
    """Mock components for the retrieval → LLM → validation pipeline."""
    conn = MagicMock(spec=sqlite3.Connection)
    retriever = MagicMock(spec=HybridRetriever)
    llm_service = MagicMock(spec=LLMService)
    validator = MagicMock(spec=EvidenceValidator)

    sample_products = [
        {"id": 1, "title": "Headphones 1", "price": 50.0},
        {"id": 2, "title": "Headphones 2", "price": 75.0},
    ]
    sample_pack = EvidencePack(product_ids=[1, 2], query="test")
    sample_answer = StructuredAnswer(
        summary="Found 2 headphones.",
        recommendations=[],
        insufficient_evidence=False,
    )
    sample_report = SupportReport(
        query="test",
        total_claims=2,
        supported_claims=2,
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


class TestIntentRouter:
    """Tests for routing logic and handler execution."""

    def test_search_is_product_first_and_never_calls_the_model(self, mock_pipeline) -> None:
        router = IntentRouter(
            conn=mock_pipeline["conn"],
            retriever=mock_pipeline["retriever"],
            llm_service=mock_pipeline["llm_service"],
            validator=mock_pipeline["validator"],
        )
        session = SessionState(session_id="test-sess")
        query = ParsedQuery(
            intent=UserIntent.PRODUCT_SEARCH,
            raw_query="wireless headphones",
            search_text="wireless headphones",
            max_price=100.0,
        )

        result = router.route(query, session)

        # Retrieval with the hard constraints, then validation; the model is never called.
        mock_pipeline["retriever"].retrieve_with_evidence.assert_called_once()
        mock_pipeline["llm_service"].generate_answer.assert_not_called()
        mock_pipeline["llm_service"].generate_answer_with_mode.assert_not_called()
        mock_pipeline["validator"].validate_answer.assert_called_once()
        sent = mock_pipeline["validator"].validate_answer.call_args.args[0]
        assert sent.summary == "" and sent.recommendations == []   # a claim-free answer

        assert result.generation.mode == "none" and result.generation.reason == "summary_not_requested"
        assert result.answer is not None
        assert result.support_report is not None
        assert result.product_ids_shown == [1, 2]
        assert session.shown_product_ids == [1, 2]
        assert session.active_filters.max_price == 100.0
        # What the optional summary will describe
        assert session.last_search.product_ids == (1, 2)
        assert session.last_search.search_text == "wireless headphones"
        assert session.last_search.filters.max_price == 100.0

    def test_search_without_products_returns_the_no_match_answer(self, mock_pipeline) -> None:
        router = IntentRouter(
            conn=mock_pipeline["conn"],
            retriever=mock_pipeline["retriever"],
            llm_service=mock_pipeline["llm_service"],
            validator=mock_pipeline["validator"],
        )
        mock_pipeline["retriever"].retrieve_with_evidence.return_value = ([], EvidencePack(product_ids=[], query=""))
        session = SessionState(session_id="s")
        result = router.route(ParsedQuery(intent=UserIntent.PRODUCT_SEARCH, raw_query="x", search_text="x"), session)
        # generate_answer_with_mode answers "no products" without a model call (LLMService case 1)
        assert mock_pipeline["llm_service"].generate_answer_with_mode.call_args.kwargs["products"] == []
        assert result.product_ids_shown == []
        assert session.last_search.product_ids == ()

    def test_refine_intent_merges_filters(self, mock_pipeline) -> None:
        router = IntentRouter(
            conn=mock_pipeline["conn"],
            retriever=mock_pipeline["retriever"],
            llm_service=mock_pipeline["llm_service"],
            validator=mock_pipeline["validator"],
        )
        session = SessionState(session_id="test-sess")
        session.active_filters = MetadataFilter(category="shoes")

        query = ParsedQuery(
            intent=UserIntent.REFINE_SEARCH,
            raw_query="under $50",
            max_price=50.0,
        )

        result = router.route(query, session)

        mock_pipeline["validator"].validate_answer.assert_called_once()
        assert session.active_filters.category == "shoes"
        assert session.active_filters.max_price == 50.0

    def test_clarify_intent_returns_question_no_retrieval(self, mock_pipeline) -> None:
        router = IntentRouter(
            conn=mock_pipeline["conn"],
            retriever=mock_pipeline["retriever"],
            llm_service=mock_pipeline["llm_service"],
            validator=mock_pipeline["validator"],
        )
        session = SessionState(session_id="test-sess")
        query = ParsedQuery(
            intent=UserIntent.CLARIFY,
            raw_query="something nice",
            clarification_question="What type of product are you looking for?",
        )

        result = router.route(query, session)

        mock_pipeline["retriever"].retrieve_with_evidence.assert_not_called()
        mock_pipeline["llm_service"].generate_answer.assert_not_called()
        mock_pipeline["validator"].validate_answer.assert_not_called()

        assert result.clarification_question == "What type of product are you looking for?"
        assert result.answer is None

    def test_smalltalk_greetings_canned_response(self, mock_pipeline) -> None:
        router = IntentRouter(
            conn=mock_pipeline["conn"],
            retriever=mock_pipeline["retriever"],
            llm_service=mock_pipeline["llm_service"],
            validator=mock_pipeline["validator"],
        )
        session = SessionState(session_id="test-sess")
        query = ParsedQuery(
            intent=UserIntent.SMALLTALK,
            raw_query="hello!",
        )

        result = router.route(query, session)

        mock_pipeline["retriever"].retrieve_with_evidence.assert_not_called()
        assert result.smalltalk_response == SMALLTALK_RESPONSES["greeting"]

    def test_out_of_scope_canned_response(self, mock_pipeline) -> None:
        router = IntentRouter(
            conn=mock_pipeline["conn"],
            retriever=mock_pipeline["retriever"],
            llm_service=mock_pipeline["llm_service"],
            validator=mock_pipeline["validator"],
        )
        session = SessionState(session_id="test-sess")
        query = ParsedQuery(
            intent=UserIntent.OUT_OF_SCOPE,
            raw_query="what is the weather in Paris?",
        )

        result = router.route(query, session)

        mock_pipeline["retriever"].retrieve_with_evidence.assert_not_called()
        assert result.smalltalk_response == OUT_OF_SCOPE_RESPONSE

    def test_question_intent_without_shown_products_asks_clarification(self, mock_pipeline) -> None:
        router = IntentRouter(
            conn=mock_pipeline["conn"],
            retriever=mock_pipeline["retriever"],
            llm_service=mock_pipeline["llm_service"],
            validator=mock_pipeline["validator"],
        )
        session = SessionState(session_id="test-sess")  # Empty shown_products

        query = ParsedQuery(
            intent=UserIntent.PRODUCT_QUESTION,
            raw_query="is it waterproof?",
        )

        result = router.route(query, session)

        assert result.clarification_question is not None
        mock_pipeline["llm_service"].generate_answer.assert_not_called()

    def test_compare_intent_without_enough_products_asks_clarification(self, mock_pipeline) -> None:
        router = IntentRouter(
            conn=mock_pipeline["conn"],
            retriever=mock_pipeline["retriever"],
            llm_service=mock_pipeline["llm_service"],
            validator=mock_pipeline["validator"],
        )
        session = SessionState(session_id="test-sess")
        session.shown_product_ids = [1]  # Only 1 product shown

        query = ParsedQuery(
            intent=UserIntent.COMPARE,
            raw_query="compare them",
            compare_product_ids=[],
        )

        result = router.route(query, session)

        assert result.clarification_question is not None
        mock_pipeline["llm_service"].generate_answer.assert_not_called()


def _product_row(pid: int) -> tuple:
    """Row in the column order selected by ``_load_products`` (currency, last, omitted)."""
    return (pid, f"EXT{pid}", f"Product {pid}", "desc", 10.0 * pid, 4.0, 10, None, "{}", "Brand", "Cat")


@pytest.fixture
def router(mock_pipeline, monkeypatch):
    """IntentRouter with a stubbed evidence-pack builder for question/compare flows."""
    monkeypatch.setattr(
        "src.agent.intent_router.assemble_evidence_pack",
        lambda conn, product_ids, **kw: EvidencePack(product_ids=list(product_ids), query=kw.get("query", "")),
    )
    return IntentRouter(
        conn=mock_pipeline["conn"],
        retriever=mock_pipeline["retriever"],
        llm_service=mock_pipeline["llm_service"],
        validator=mock_pipeline["validator"],
    )


def _session_with_results(ids: list[int]) -> SessionState:
    session = SessionState(session_id="follow-up")
    session.set_search_results("wireless headphones", ids)
    return session


class TestFollowUpResolution:
    """Follow-ups resolve against what the user just saw, or ask instead of guessing."""

    def test_refine_keeps_original_search_context(self, router, mock_pipeline) -> None:
        session = SessionState(session_id="s")
        router.route(
            ParsedQuery(intent=UserIntent.PRODUCT_SEARCH, raw_query="wireless headphones",
                        search_text="wireless headphones"),
            session,
        )
        router.route(
            ParsedQuery(intent=UserIntent.REFINE_SEARCH, raw_query="only black ones",
                        search_text="only black ones"),
            session,
        )

        last_call = mock_pipeline["retriever"].retrieve_with_evidence.call_args
        assert last_call.kwargs["query"] == "wireless headphones only black ones"

    def test_follow_up_positions_follow_the_displayed_order(self, router, mock_pipeline) -> None:
        """'The first one' is the first card shown; a later summary never reorders the cards."""
        from src.llm.schemas import ProductRecommendation

        session = SessionState(session_id="s")
        router.route(
            ParsedQuery(intent=UserIntent.PRODUCT_SEARCH, raw_query="headphones", search_text="headphones"),
            session,
        )
        assert session.last_result_ids == [1, 2]
        assert session.shown_product_ids == [1, 2]

        answer = StructuredAnswer(
            summary="Two picks.",
            recommendations=[
                ProductRecommendation(product_id=2, why_it_matches=[], watch_outs=[]),
                ProductRecommendation(product_id=999, why_it_matches=[], watch_outs=[]),  # not retrieved
                ProductRecommendation(product_id=1, why_it_matches=[], watch_outs=[]),
            ],
        )
        mock_pipeline["validator"].validate_answer.return_value = (answer, mock_pipeline["sample_report"])
        mock_pipeline["conn"].cursor.return_value.execute.return_value.fetchall.return_value = [
            _product_row(2), _product_row(1),
        ]
        result = router.summarise(session.last_search)
        assert result.product_ids_shown == [1, 2]
        assert session.last_result_ids == [1, 2]

    def test_new_search_resets_search_context(self, router, mock_pipeline) -> None:
        session = _session_with_results([1, 2])
        router.route(
            ParsedQuery(intent=UserIntent.PRODUCT_SEARCH, raw_query="laptops", search_text="laptops"),
            session,
        )
        assert mock_pipeline["retriever"].retrieve_with_evidence.call_args.kwargs["query"] == "laptops"
        assert session.active_search_text == "laptops"

    def test_ambiguous_question_over_many_results_asks(self, router, mock_pipeline) -> None:
        session = _session_with_results([1, 2, 3])
        result = router.route(
            ParsedQuery(intent=UserIntent.PRODUCT_QUESTION, raw_query="is it waterproof?"), session
        )
        assert result.clarification_question is not None
        assert "first one" in result.clarification_question
        mock_pipeline["llm_service"].generate_answer.assert_not_called()

    def test_positional_question_targets_that_result_and_validates(self, router, mock_pipeline) -> None:
        mock_pipeline["conn"].cursor.return_value.execute.return_value.fetchall.return_value = [_product_row(2)]
        session = _session_with_results([1, 2, 3])

        result = router.route(
            ParsedQuery(intent=UserIntent.PRODUCT_QUESTION, raw_query="is the second one waterproof?"),
            session,
        )

        assert result.product_ids_shown == [2]
        assert session.focused_product_id == 2
        sql_args = mock_pipeline["conn"].cursor.return_value.execute.call_args.args[1]
        assert sql_args == [2]
        assert mock_pipeline["llm_service"].generate_answer.call_args.kwargs["products"][0]["id"] == 2
        mock_pipeline["validator"].validate_answer.assert_called_once()

    def test_pronoun_follow_up_uses_focused_product(self, router, mock_pipeline) -> None:
        mock_pipeline["conn"].cursor.return_value.execute.return_value.fetchall.return_value = [_product_row(3)]
        session = _session_with_results([1, 2, 3])
        session.focused_product_id = 3

        result = router.route(
            ParsedQuery(intent=UserIntent.PRODUCT_QUESTION, raw_query="does it run small?"), session
        )
        assert result.product_ids_shown == [3]

    def test_single_result_question_needs_no_reference(self, router, mock_pipeline) -> None:
        mock_pipeline["conn"].cursor.return_value.execute.return_value.fetchall.return_value = [_product_row(7)]
        session = _session_with_results([7])
        result = router.route(
            ParsedQuery(intent=UserIntent.PRODUCT_QUESTION, raw_query="is it durable?"), session
        )
        assert result.product_ids_shown == [7]

    def test_out_of_range_position_asks(self, router, mock_pipeline) -> None:
        session = _session_with_results([1, 2])
        result = router.route(
            ParsedQuery(intent=UserIntent.PRODUCT_QUESTION, raw_query="is the fifth one good?"), session
        )
        assert "only showed 2" in result.clarification_question
        mock_pipeline["llm_service"].generate_answer.assert_not_called()

    def test_compare_these_two_uses_top_ranked_results(self, router, mock_pipeline) -> None:
        mock_pipeline["conn"].cursor.return_value.execute.return_value.fetchall.return_value = [
            _product_row(6), _product_row(5),  # SQL order differs from rank order
        ]
        session = _session_with_results([5, 6, 7, 8, 9])

        result = router.route(
            ParsedQuery(intent=UserIntent.COMPARE, raw_query="compare these two"), session
        )

        assert result.product_ids_shown == [5, 6]
        products = mock_pipeline["llm_service"].generate_answer.call_args.kwargs["products"]
        assert [p["id"] for p in products] == [5, 6]
        mock_pipeline["validator"].validate_answer.assert_called_once()

    def test_compare_by_position(self, router, mock_pipeline) -> None:
        mock_pipeline["conn"].cursor.return_value.execute.return_value.fetchall.return_value = [
            _product_row(5), _product_row(7),
        ]
        session = _session_with_results([5, 6, 7])
        result = router.route(
            ParsedQuery(intent=UserIntent.COMPARE, raw_query="compare #1 and #3"), session
        )
        assert result.product_ids_shown == [5, 7]

    def test_compare_drops_ids_never_shown(self, router, mock_pipeline) -> None:
        mock_pipeline["conn"].cursor.return_value.execute.return_value.fetchall.return_value = [
            _product_row(5), _product_row(6),
        ]
        session = _session_with_results([5, 6])
        result = router.route(
            ParsedQuery(intent=UserIntent.COMPARE, raw_query="compare them", compare_product_ids=[998, 999]),
            session,
        )
        assert result.product_ids_shown == [5, 6]
