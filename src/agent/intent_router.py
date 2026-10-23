"""
Intent router and handler implementations for Self Search (Phase 7).

Dispatches ``ParsedQuery`` objects to the appropriate handler based on
``UserIntent``. Each handler coordinates the retrieval → LLM → evidence-validation
pipeline where applicable, using the existing Phase 4–6 infrastructure.

The intent router NEVER bypasses evidence validation for answer-generating intents.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

import structlog

from src.agent.constraints import ConstraintResolver, Resolution
from src.agent.query_parser import extract_compare_count, extract_ordinal_references
from src.agent.schemas import AgentResponse, ConstraintReport, ParsedQuery, UserIntent
from src.agent.session import SearchSnapshot, SessionState
from src.evidence.evidence_pack import EvidencePack, assemble_evidence_pack
from src.evidence.report import SupportReport
from src.evidence.validator import EvidenceValidator
from src.llm.schemas import GenerationInfo, StructuredAnswer
from src.llm.service import LLMService
from src.retrieval.filters import MetadataFilter
from src.retrieval.hybrid import HybridRetriever

logger = structlog.get_logger("shelfsense.agent.intent_router")

# ---------------------------------------------------------------------------
# Canned responses for non-retrieval intents
# ---------------------------------------------------------------------------

SMALLTALK_RESPONSES: dict[str, str] = {
    "greeting": (
        "Hello! I'm Self Search, your evidence-grounded product discovery assistant. "
        "Tell me what you're looking for and I'll find matching products with real "
        "review evidence. What can I help you find today?"
    ),
    "thanks": (
        "You're welcome! Let me know if you'd like to search for more products "
        "or have any questions about the ones I've shown you."
    ),
    "farewell": (
        "Goodbye! Feel free to come back anytime you need help finding products. "
        "Have a great day!"
    ),
    "default": (
        "I'm here to help you find products! Try describing what you're looking for, "
        "including any preferences for category, brand, price range, or features."
    ),
}

OUT_OF_SCOPE_RESPONSE = (
    "I'm a product discovery assistant and can only help with shopping-related questions. "
    "Try asking me to find products, compare items, or answer questions about product reviews. "
    "For example: 'Find me running shoes under ₹3000 with good reviews.'"
)


def _classify_smalltalk_type(raw_query: str) -> str:
    """Classify smalltalk subtype for canned response selection."""
    lower = raw_query.strip().lower()
    greetings = {"hi", "hello", "hey", "howdy", "good morning", "good afternoon", "good evening"}
    farewells = {"bye", "goodbye", "see ya", "see you", "farewell"}
    thanks = {"thanks", "thank you", "thank", "cheers", "appreciated"}

    for word in greetings:
        if word in lower:
            return "greeting"
    for word in farewells:
        if word in lower:
            return "farewell"
    for word in thanks:
        if word in lower:
            return "thanks"
    return "default"


@dataclass
class HandlerResult:
    """Internal result from an intent handler."""

    answer: Optional[StructuredAnswer] = None
    support_report: Optional[SupportReport] = None
    clarification_question: Optional[str] = None
    smalltalk_response: Optional[str] = None
    product_ids_shown: list[int] = None  # type: ignore[assignment]
    # Phase 9: the exact pack given to EvidenceValidator, and how the answer was generated.
    evidence_pack: Optional[EvidencePack] = None
    generation: Optional[GenerationInfo] = None
    # Relevance fix: interpreted hard constraints for search/refine turns.
    constraint_report: Optional[ConstraintReport] = None

    def __post_init__(self) -> None:
        if self.product_ids_shown is None:
            self.product_ids_shown = []


class IntentRouter:
    """Routes parsed queries to intent-specific handlers.

    All answer-generating handlers pass through EvidenceValidator before
    returning results. Non-answer intents (CLARIFY, SMALLTALK, OUT_OF_SCOPE)
    return canned responses without retrieval or LLM generation.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        retriever: HybridRetriever,
        llm_service: LLMService,
        validator: EvidenceValidator,
        search_limit: int = 10,
        max_reviews_per_product: int = 2,
        token_budget: int = 800,
        answer_products: int = 3,
        snippet_words: int = 60,
    ) -> None:
        """
        Args:
            search_limit: Products retrieved and shown per search.
            max_reviews_per_product: Review snippets per product sent to the model.
            token_budget: Evidence-pack budget (a CPU-only model reads about 25 tokens/s).
            answer_products: An on-demand summary covers the top N results only; they are
                the only ones with evidence, so every claim can be checked.
            snippet_words: Each review snippet is truncated to this many words.
        """
        self.conn = conn
        self.retriever = retriever
        self.llm_service = llm_service
        self.validator = validator
        self.search_limit = search_limit
        self.max_reviews_per_product = max_reviews_per_product
        self.token_budget = token_budget
        self.answer_products = answer_products
        self.snippet_words = snippet_words
        self.constraints = ConstraintResolver(conn)

    def route(
        self,
        parsed_query: ParsedQuery,
        session: SessionState,
        selected_product_ids: Optional[list[int]] = None,
    ) -> HandlerResult:
        """Dispatch a parsed query to the appropriate intent handler.

        Args:
            parsed_query: Structured parse of the user message.
            session: Current conversation session state.
            selected_product_ids: Products the user explicitly picked in the UI
                (Phase 9): one for PRODUCT_QUESTION, 2–4 for COMPARE. The caller
                must have checked they were shown in this session.

        Returns:
            HandlerResult with the appropriate response data.
        """
        intent = parsed_query.intent
        logger.info(
            "intent_routing",
            intent=intent.value,
            session_id=session.session_id,
        )

        if intent == UserIntent.REFINE_SEARCH:
            return self._handle_refine(parsed_query, session)
        if intent == UserIntent.PRODUCT_QUESTION:
            return self._handle_question(parsed_query, session, selected_product_ids=selected_product_ids)
        if intent == UserIntent.COMPARE:
            return self._handle_compare(parsed_query, session, selected_product_ids=selected_product_ids)

        handlers = {
            UserIntent.CLARIFY: self._handle_clarify,
            UserIntent.SMALLTALK: self._handle_smalltalk,
            UserIntent.OUT_OF_SCOPE: self._handle_out_of_scope,
        }
        handler = handlers.get(intent)
        if handler is None:  # PRODUCT_SEARCH, and the default for any unknown intent
            return self._handle_search(parsed_query, session)
        return handler(parsed_query, session)

    # ------------------------------------------------------------------
    # Follow-up reference resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_question_target(
        parsed_query: ParsedQuery,
        session: SessionState,
    ) -> tuple[Optional[int], Optional[str]]:
        """Resolve which shown product a follow-up question refers to.

        Order: explicit position ("the second one") in the latest results →
        the product already under discussion → the only result shown.
        If several products were shown and none is singled out, ask rather than guess.

        Returns:
            Tuple of (product_id, None) on success or (None, clarification_question).
        """
        candidates = session.last_result_ids or session.shown_product_ids
        if not candidates:
            return None, (
                "I'm not sure which product you're asking about. "
                "Could you search for a product first, or specify which product "
                "you'd like to know more about?"
            )

        positions = extract_ordinal_references(parsed_query.raw_query)
        if positions:
            pos = positions[0]
            if pos >= len(candidates):
                return None, (
                    f"I only showed {len(candidates)} product(s) in the latest results. "
                    "Which one would you like to know about?"
                )
            return candidates[pos], None

        if session.focused_product_id is not None:
            return session.focused_product_id, None

        if len(candidates) == 1:
            return candidates[0], None

        return None, (
            f"I showed {len(candidates)} products. Which one are you asking about? "
            "You can refer to it by position, for example: 'is the first one waterproof?'"
        )

    @staticmethod
    def _resolve_compare_targets(
        parsed_query: ParsedQuery,
        session: SessionState,
    ) -> list[int]:
        """Resolve which shown products to compare (at most 4).

        Positional references in the user's own words take precedence
        ("compare these two", "#1 and #3"). Product IDs proposed by the LLM
        parser are only accepted if they were actually shown in this session,
        since the parser never sees catalog IDs. Otherwise defaults to the
        top results of the latest search.
        """
        candidates = session.last_result_ids or session.shown_product_ids
        raw = parsed_query.raw_query

        count = extract_compare_count(raw)
        positions = extract_ordinal_references(raw)
        llm_ids = [pid for pid in parsed_query.compare_product_ids if pid in session.shown_product_ids]
        if len(llm_ids) != len(parsed_query.compare_product_ids):
            logger.warning(
                "compare_ids_not_shown_dropped",
                proposed=parsed_query.compare_product_ids,
                kept=llm_ids,
            )

        if count:
            ids = candidates[:count]
        elif positions:
            ids = [candidates[p] for p in positions if -len(candidates) <= p < len(candidates)]
        elif llm_ids:
            ids = llm_ids
        else:
            ids = candidates[:4]

        return list(dict.fromkeys(ids))[:4]

    # ------------------------------------------------------------------
    # Intent handlers
    # ------------------------------------------------------------------

    def _handle_search(
        self,
        parsed_query: ParsedQuery,
        session: SessionState,
    ) -> HandlerResult:
        """Handle PRODUCT_SEARCH product-first: constrained retrieval only, no model call.

        Hard constraints are resolved first and enforced inside retrieval; a
        budget in a currency the catalogue doesn't use returns a clarification
        without retrieving anything. The results are returned as soon as
        retrieval finishes, with an empty (claim-free) answer; a model summary of
        them is generated only on request (``summarise``).
        """
        filters, resolution, blocked = self._apply_constraints(parsed_query, session)
        if blocked is not None:
            return blocked
        if parsed_query.intent == UserIntent.REFINE_SEARCH and session.active_search_text:
            # Keep the original need ("wireless headphones") when refining ("only black ones")
            search_text = " ".join(
                t for t in (session.active_search_text, parsed_query.search_text) if t
            )
        else:
            search_text = parsed_query.search_text or parsed_query.raw_query

        products, evidence_pack = self.retriever.retrieve_with_evidence(
            query=search_text,
            limit=self.search_limit,
            filters=filters,
            max_reviews_per_product=self.max_reviews_per_product,
            token_budget=self.token_budget,
            evidence_products=self.answer_products,
            max_words=self.snippet_words,
        )

        if products:
            answer = StructuredAnswer(summary="", recommendations=[])
            generation = GenerationInfo(mode="none", reason="summary_not_requested")
        else:  # the "no matching products" answer; never calls the model
            answer, generation = self.llm_service.generate_answer_with_mode(
                query=search_text, products=[], evidence_pack=evidence_pack, filters=filters,
            )
        # The claim-free answer still goes through the validator, like every answer.
        validated_answer, report = self.validator.validate_answer(answer, evidence_pack)

        product_ids = [p["id"] for p in products]
        session.set_search_results(search_text, product_ids)
        constraint_report = self.constraints.report(filters, resolution)
        session.last_search = SearchSnapshot(
            intent=parsed_query.intent, search_text=search_text, filters=filters,
            product_ids=tuple(product_ids), constraints=constraint_report,
        )

        logger.info("search_complete", products_found=len(products))

        return HandlerResult(
            answer=validated_answer,
            support_report=report,
            evidence_pack=evidence_pack,
            generation=generation,
            product_ids_shown=product_ids,
            constraint_report=constraint_report,
        )

    def summarise(self, snapshot: SearchSnapshot) -> HandlerResult:
        """Generate the optional, evidence-grounded model summary of a search's results.

        Covers the top ``answer_products`` results with their own review evidence;
        the answer passes through ``EvidenceValidator`` like every other answer.
        Display order stays the search's own (``product_ids_shown``).
        """
        ids = list(snapshot.product_ids[: self.answer_products])
        products = self._load_products(ids)
        evidence_pack = assemble_evidence_pack(
            conn=self.conn,
            product_ids=ids,
            query=snapshot.search_text,
            max_reviews_per_product=self.max_reviews_per_product,
            token_budget=self.token_budget,
            max_words=self.snippet_words,
        )
        answer, generation = self.llm_service.generate_answer_with_mode(
            query=snapshot.search_text,
            products=products,
            evidence_pack=evidence_pack,
            filters=snapshot.filters,
        )
        validated_answer, report = self.validator.validate_answer(answer, evidence_pack)
        logger.info("summary_complete", products=len(ids), mode=generation.mode, support_rate=report.support_rate)
        return HandlerResult(
            answer=validated_answer,
            support_report=report,
            evidence_pack=evidence_pack,
            generation=generation,
            product_ids_shown=list(snapshot.product_ids),
            constraint_report=snapshot.constraints,
        )

    def _load_products(self, product_ids: list[int]) -> list[dict[str, Any]]:
        """Product records for the prompt, in the given order (unknown IDs are skipped).

        Synthetic price estimates are deliberately not loaded: they never reach the model.
        """
        if not product_ids:
            return []
        placeholders = ",".join("?" for _ in product_ids)
        rows = self.conn.cursor().execute(
            f"""
            SELECT p.id, p.external_id, p.title, p.description, p.price,
                   p.avg_rating, p.rating_count, p.image_url, p.attributes_json,
                   b.name AS brand_name, c.name AS category_name, p.currency
            FROM products p
            LEFT JOIN brands b ON p.brand_id = b.id
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.id IN ({placeholders})
            """,
            list(product_ids),
        ).fetchall()
        keys = ("id", "external_id", "title", "description", "price", "avg_rating", "rating_count",
                "image_url", "attributes_json", "brand_name", "category_name", "currency")
        by_id = {row[0]: dict(zip(keys, tuple(row))) for row in rows}
        return [by_id[pid] for pid in product_ids if pid in by_id]

    def _apply_constraints(
        self,
        parsed_query: ParsedQuery,
        session: SessionState,
    ) -> tuple[MetadataFilter, Resolution, Optional[HandlerResult]]:
        """Resolve hard constraints, merge them into the session, and detect an unusable budget.

        Returns:
            (filters, resolution, clarification result or None). When the budget's
            currency isn't used by the catalogue, the other constraints are still
            merged (so a follow-up keeps them) but nothing is retrieved or generated.
        """
        resolution = self.constraints.resolve(parsed_query)
        filters = session.merge_filters(parsed_query, resolution.fields)
        # Recomputed every turn (also for budgets carried over from earlier turns):
        # INR bounds may use synthetic estimates only while the feature is enabled.
        filters.use_price_estimates = (
            self.constraints.estimates_active and (filters.price_currency or "").upper() == "INR"
        )
        if not resolution.price_blocked:
            return filters, resolution, None
        report = self.constraints.report(filters, resolution)
        return filters, resolution, HandlerResult(
            clarification_question=self.constraints.clarification(resolution, report),
            constraint_report=report,
        )

    def _handle_refine(
        self,
        parsed_query: ParsedQuery,
        session: SessionState,
    ) -> HandlerResult:
        """Handle REFINE_SEARCH: merge new constraints and re-run search."""
        # Refinement uses the same pipeline as search, with merged filters
        return self._handle_search(parsed_query, session)

    def _handle_question(
        self,
        parsed_query: ParsedQuery,
        session: SessionState,
        selected_product_ids: Optional[list[int]] = None,
    ) -> HandlerResult:
        """Handle PRODUCT_QUESTION: answer a question about a specific product."""
        if selected_product_ids and len(selected_product_ids) == 1:
            target_product_id, clarification = selected_product_ids[0], None
        else:
            target_product_id, clarification = self._resolve_question_target(parsed_query, session)
        if target_product_id is None:
            return HandlerResult(clarification_question=clarification)
        session.focused_product_id = target_product_id

        # Retrieve evidence for the specific product
        evidence_pack = assemble_evidence_pack(
            conn=self.conn,
            product_ids=[target_product_id],
            query=parsed_query.raw_query,
            max_reviews_per_product=self.max_reviews_per_product * 2,
            token_budget=self.token_budget,
            max_words=self.snippet_words,
        )

        found = self._load_products([target_product_id])
        if not found:
            return HandlerResult(
                clarification_question=(
                    "I couldn't find that product in the catalog. "
                    "Could you try searching for it again?"
                ),
            )

        product = found[0]

        answer, generation = self.llm_service.generate_answer_with_mode(
            query=parsed_query.raw_query,
            products=[product],
            evidence_pack=evidence_pack,
        )

        validated_answer, report = self.validator.validate_answer(answer, evidence_pack)

        return HandlerResult(
            answer=validated_answer,
            support_report=report,
            evidence_pack=evidence_pack,
            generation=generation,
            product_ids_shown=[target_product_id],
        )

    def _handle_compare(
        self,
        parsed_query: ParsedQuery,
        session: SessionState,
        selected_product_ids: Optional[list[int]] = None,
    ) -> HandlerResult:
        """Handle COMPARE: side-by-side comparison of 2–4 products."""
        if selected_product_ids and len(selected_product_ids) >= 2:
            compare_ids = list(selected_product_ids)[:4]
        else:
            compare_ids = self._resolve_compare_targets(parsed_query, session)

        if len(compare_ids) < 2:
            return HandlerResult(
                clarification_question=(
                    "I need at least 2 products to compare. "
                    "Could you search for products first, or specify which "
                    "products you'd like to compare?"
                ),
            )

        # Limit to 4 products
        compare_ids = compare_ids[:4]

        # Product records, in the order the user referred to them
        products = self._load_products(compare_ids)

        if len(products) < 2:
            return HandlerResult(
                clarification_question=(
                    "I couldn't find enough products for comparison. "
                    "Could you search for products first?"
                ),
            )

        evidence_pack = assemble_evidence_pack(
            conn=self.conn,
            product_ids=compare_ids,
            query=parsed_query.raw_query,
            max_reviews_per_product=self.max_reviews_per_product,
            token_budget=self.token_budget,
            max_words=self.snippet_words,
        )

        answer, generation = self.llm_service.generate_answer_with_mode(
            query=parsed_query.raw_query,
            products=products,
            evidence_pack=evidence_pack,
        )

        validated_answer, report = self.validator.validate_answer(answer, evidence_pack)

        return HandlerResult(
            answer=validated_answer,
            support_report=report,
            evidence_pack=evidence_pack,
            generation=generation,
            product_ids_shown=compare_ids,
        )

    def _handle_clarify(
        self,
        parsed_query: ParsedQuery,
        session: SessionState,
    ) -> HandlerResult:
        """Handle CLARIFY: return clarification question without retrieval."""
        question = parsed_query.clarification_question or (
            "I'd like to help you find the right product. "
            "Could you describe what you're looking for, including any "
            "preferences for category, brand, budget, or features?"
        )
        return HandlerResult(clarification_question=question)

    def _handle_smalltalk(
        self,
        parsed_query: ParsedQuery,
        session: SessionState,
    ) -> HandlerResult:
        """Handle SMALLTALK: return a friendly canned response."""
        subtype = _classify_smalltalk_type(parsed_query.raw_query)
        response = SMALLTALK_RESPONSES.get(subtype, SMALLTALK_RESPONSES["default"])
        return HandlerResult(smalltalk_response=response)

    def _handle_out_of_scope(
        self,
        parsed_query: ParsedQuery,
        session: SessionState,
    ) -> HandlerResult:
        """Handle OUT_OF_SCOPE: steer user back to product discovery."""
        return HandlerResult(smalltalk_response=OUT_OF_SCOPE_RESPONSE)
