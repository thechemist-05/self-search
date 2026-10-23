"""
Agent controller orchestrating the Self Search conversational pipeline (Phase 7).

Coordinates:
1. Session management (load/create session, history tracking)
2. Query parsing (user intent + constraint extraction)
3. Intent routing (search, refine, question, compare, clarify, smalltalk)
4. Evidence-grounded generation and validation
5. Updating session state with results
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any, Optional, Sequence

import structlog

from src.agent.intent_router import HandlerResult, IntentRouter
from src.agent.presentation import ProductPresenter, evidence_snippets, order_result_ids
from src.agent.query_parser import QueryParser
from src.agent.schemas import AgentResponse, ParsedQuery, UserIntent
from src.agent.session import SessionManager, SessionState
from src.config.settings import get_settings
from src.evidence.validator import EvidenceValidator
from src.llm.deadline import model_time_budget
from src.llm.provider import LLMProvider
from src.llm.service import LLMService
from src.retrieval.hybrid import HybridRetriever

logger = structlog.get_logger("shelfsense.agent.controller")


class InvalidProductSelectionError(ValueError):
    """Raised when ``product_ids`` is malformed or names products not shown in the session."""


class SessionNotFoundError(LookupError):
    """Raised when a request names a session that does not exist."""


class NothingToSummariseError(ValueError):
    """Raised when a summary is requested but the session's latest search has no results,
    or the results it names are no longer the session's latest results."""


class AgentController:
    """Top-level agent orchestrator for conversational product discovery.

    Ensures that every user query is understood, routed, and if factual answers
    are produced, strictly validated against retrieved evidence before being
    returned to the user.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        llm_provider: LLMProvider,
        retriever: HybridRetriever,
        llm_service: LLMService,
        validator: EvidenceValidator,
        session_manager: Optional[SessionManager] = None,
        query_parser: Optional[QueryParser] = None,
        intent_router: Optional[IntentRouter] = None,
    ) -> None:
        self.conn = conn
        self.llm_provider = llm_provider
        self.retriever = retriever
        self.llm_service = llm_service
        self.validator = validator

        self.presenter = ProductPresenter(conn)
        self.session_manager = session_manager or SessionManager()
        # One summary at a time per session: a repeated click waits, then gets the cached answer.
        self._summary_locks: dict[str, threading.Lock] = {}
        self._summary_locks_guard = threading.Lock()
        self.query_parser = query_parser or QueryParser(llm_provider=llm_provider)
        self.intent_router = intent_router or IntentRouter(
            conn=conn,
            retriever=retriever,
            llm_service=llm_service,
            validator=validator,
        )

    def handle_message(
        self,
        user_message: str,
        session_id: Optional[str] = None,
        product_ids: Optional[list[int]] = None,
    ) -> AgentResponse:
        """Process one message within a single model-time budget (``SHELFSENSE_OLLAMA_TIMEOUT``).

        Query parsing and answer generation share the budget, so a message never waits
        for sequential model calls longer than that; when it runs out the safe fallback
        answers. See ``_handle_message`` for the pipeline, arguments and errors.
        """
        with model_time_budget(get_settings().ollama_timeout):
            return self._handle_message(user_message, session_id, product_ids)

    def _handle_message(
        self,
        user_message: str,
        session_id: Optional[str] = None,
        product_ids: Optional[list[int]] = None,
    ) -> AgentResponse:
        """Process an incoming user message through the complete agent pipeline.

        Args:
            user_message: Raw text from the user.
            session_id: Optional ID of an existing conversation session.
            product_ids: Optional explicit product selection from the UI (Phase 9).
                One ID asks a question about that product; 2–4 compare them.
                Every ID must have been shown in this session.

        Returns:
            AgentResponse containing the classified intent, validated answer
            (if applicable), clarification or smalltalk response, and session state.

        Raises:
            InvalidProductSelectionError: ``product_ids`` is malformed or names
                products not shown in this session.
        """
        # Validate before touching session state, so a bad request changes nothing.
        if product_ids is not None:
            self._validate_product_selection(product_ids, session_id)

        session = self.session_manager.get_or_create(session_id)
        logger.info(
            "agent_message_received",
            session_id=session.session_id,
            message_len=len(user_message),
        )

        # 1. Fetch prior history for context (before adding current turn)
        prior_history = session.get_recent_history_for_prompt()

        # 2. Record user turn in session history
        session.add_turn(role="user", content=user_message)

        # 3. Parse query to understand user intent and constraints
        parsed_query: ParsedQuery = self.query_parser.parse(
            user_message=user_message,
            conversation_history=prior_history,
        )

        # An explicit UI selection decides the intent, unless the message is
        # smalltalk or out of scope (those never produce product answers).
        if product_ids and parsed_query.intent not in (UserIntent.SMALLTALK, UserIntent.OUT_OF_SCOPE):
            selected_intent = UserIntent.PRODUCT_QUESTION if len(product_ids) == 1 else UserIntent.COMPARE
            parsed_query = parsed_query.model_copy(update={"intent": selected_intent})

        session.last_parsed_query = parsed_query
        session.last_intent = parsed_query.intent

        # 4. Route query to appropriate handler
        try:
            result: HandlerResult = self.intent_router.route(
                parsed_query=parsed_query,
                session=session,
                selected_product_ids=product_ids,
            )
        except Exception as e:
            logger.exception("intent_route_error", error=str(e), intent=parsed_query.intent.value)
            # Safe degradation fallback: ask for clarification or retry
            result = HandlerResult(
                clarification_question=(
                    "I encountered an issue processing your request. "
                    "Could you rephrase what product you're looking for?"
                ),
            )

        # 5. Formulate assistant response summary for session history
        assistant_content = ""
        if result.answer is not None:
            assistant_content = getattr(result.answer, "summary", "") or "Here are the matching products."
        elif result.clarification_question:
            assistant_content = result.clarification_question
        elif result.smalltalk_response:
            assistant_content = result.smalltalk_response

        session.add_turn(
            role="assistant",
            content=assistant_content,
            intent=parsed_query.intent.value,
        )

        # 6. Format filters and reports for API response
        return self._response(session, parsed_query.intent, result, session.active_filters)

    def summarise_results(
        self,
        session_id: str,
        result_product_ids: Optional[Sequence[int]] = None,
    ) -> AgentResponse:
        """Optional model summary of the session's latest search results (on request only).

        The summary covers the top results with their own review evidence and is
        validated like every answer. The response carries the same products in the
        same order as the search, now with the summary and its claims. A summary
        produced by the model is cached per result set, and concurrent requests for
        one session are serialised, so repeated clicks never call the model twice.

        Args:
            session_id: An existing session that has run a search.
            result_product_ids: The results the client is showing. When given, they
                must equal the session's latest results, so a summary never describes
                a different result set.

        Raises:
            SessionNotFoundError: Unknown session.
            NothingToSummariseError: No results to summarise, or the results changed.
        """
        session = self.session_manager.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"Session '{session_id}' not found")
        snapshot = session.last_search
        if snapshot is None or not snapshot.product_ids:
            raise NothingToSummariseError("There are no search results to summarise")
        if result_product_ids is not None and tuple(result_product_ids) != snapshot.product_ids:
            raise NothingToSummariseError("These results are no longer the latest search; search again")

        with self._summary_locks_guard:
            lock = self._summary_locks.setdefault(session.session_id, threading.Lock())
        with lock:
            cached = session.summary_cache.get(snapshot.key)
            if cached is not None:
                logger.info("summary_cache_hit", session_id=session.session_id)
                return cached
            with model_time_budget(get_settings().ollama_timeout):
                result = self.intent_router.summarise(snapshot)
            response = self._response(session, snapshot.intent, result, snapshot.filters, keep_order=True)
            # Only a model-written summary is cached; after a fallback the user can retry.
            if result.generation is not None and result.generation.mode == "llm":
                session.summary_cache[snapshot.key] = response
            return response

    def _response(
        self,
        session: SessionState,
        intent: UserIntent,
        result: HandlerResult,
        filters: Any,
        keep_order: bool = False,
    ) -> AgentResponse:
        """AgentResponse for a handler result (shared by messages and summaries)."""
        answer_dict = result.answer.model_dump() if result.answer is not None else None
        support_report_dict = result.support_report.to_dict() if result.support_report is not None else None
        return AgentResponse(
            session_id=session.session_id,
            intent=intent,
            answer=answer_dict,
            clarification_question=result.clarification_question,
            smalltalk_response=result.smalltalk_response,
            support_report=support_report_dict,
            active_filters={
                "category": filters.category,
                "brand": filters.brand,
                "min_price": filters.min_price,
                "max_price": filters.max_price,
                "min_rating": filters.min_rating,
            },
            shown_product_ids=list(session.shown_product_ids),
            parsing_confidence=session.last_parsed_query.confidence if session.last_parsed_query else 1.0,
            **self._presentation(result, filters, keep_order=keep_order),
        )

    def _validate_product_selection(self, product_ids: list[int], session_id: Optional[str]) -> None:
        """Reject selections that are malformed or name products not shown in this session."""
        if not 1 <= len(product_ids) <= 4:
            raise InvalidProductSelectionError("product_ids must contain 1 to 4 products")
        if len(set(product_ids)) != len(product_ids):
            raise InvalidProductSelectionError("product_ids must not contain duplicates")
        session = self.session_manager.get(session_id) if session_id else None
        if session is None:
            raise InvalidProductSelectionError("product_ids requires an existing session that has shown products")
        unshown = [pid for pid in product_ids if pid not in session.shown_product_ids]
        if unshown:
            raise InvalidProductSelectionError(f"Products {unshown} were not shown in this session")

    def _presentation(
        self,
        result: HandlerResult,
        active_filters: Any = None,
        keep_order: bool = False,
    ) -> dict[str, Any]:
        """Phase 9 response fields: DB-backed cards, validated evidence, generation,
        and the interpreted hard constraints.

        With ``keep_order`` (a summary of results already on screen) the cards keep
        their order; recommended products are only marked, not moved.
        """
        fields: dict[str, Any] = {}
        if result.constraint_report is not None:
            fields["constraints"] = result.constraint_report
        if result.answer is not None:
            ordered, recommended = order_result_ids(result.product_ids_shown, result.answer)
            if keep_order:
                ordered = list(dict.fromkeys(result.product_ids_shown))
            ranks = {pid: i + 1 for i, pid in enumerate(result.product_ids_shown)}
            fields["result_product_ids"] = ordered
            # Per-card constraint evidence only where constraints governed retrieval.
            card_filters = active_filters if result.constraint_report is not None else None
            try:
                fields["products"] = self.presenter.cards(ordered, recommended, ranks, card_filters)
            except Exception as e:  # cards are display data; never fail the validated answer
                logger.exception("presentation_cards_failed", error=str(e))
            fields["evidence"] = evidence_snippets(result.evidence_pack)
            fields["generation"] = result.generation
        return fields
