"""
Session state management for Self Search conversations (Phase 7).

Provides in-memory, per-conversation state tracking including:
- Active metadata filters (accumulated across refinements)
- Shown product IDs (for "show me something different")
- Conversation history (sliding window)
- Last parsed query and intent

Sessions are not persisted across server restarts in Phase 7.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from src.agent.schemas import ParsedQuery, UserIntent
from src.retrieval.filters import MetadataFilter

logger = structlog.get_logger("shelfsense.agent.session")


@dataclass(frozen=True)
class SearchSnapshot:
    """What the latest search showed, so an optional summary describes exactly those results."""

    intent: UserIntent
    search_text: str
    filters: MetadataFilter
    product_ids: tuple[int, ...]
    constraints: Optional[Any] = None  # ConstraintReport shown with the results

    @property
    def key(self) -> tuple[Any, ...]:
        """Identifies this result set; equal keys mean an identical summary request."""
        return (self.search_text, self.product_ids, repr(self.filters))


@dataclass
class SessionState:
    """Mutable state for one conversation session."""

    session_id: str
    active_filters: MetadataFilter = field(default_factory=MetadataFilter)
    shown_product_ids: list[int] = field(default_factory=list)
    conversation_history: list[dict[str, Any]] = field(default_factory=list)
    last_intent: Optional[UserIntent] = None
    last_parsed_query: Optional[ParsedQuery] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    max_history_turns: int = 20
    # Product IDs presented by the most recent search/refine, in order, used to
    # resolve "the second one" / "compare these two" against what the user just saw.
    last_result_ids: list[int] = field(default_factory=list)
    # Product the user is currently asking about (set by PRODUCT_QUESTION).
    focused_product_id: Optional[int] = None
    # Accumulated retrieval text, so a refinement like "only black ones" keeps
    # the original "wireless headphones" context.
    active_search_text: str = ""
    # The latest search's results, and model summaries already generated for a result set
    # (SearchSnapshot.key -> AgentResponse), so a repeated request never calls the model again.
    last_search: Optional[SearchSnapshot] = None
    summary_cache: dict[tuple[Any, ...], Any] = field(default_factory=dict)

    def set_search_results(
        self,
        search_text: str,
        product_ids: list[int],
        presented_ids: Optional[list[int]] = None,
    ) -> None:
        """Record the latest search results and reset the focused product.

        Args:
            search_text: Retrieval text used for this search.
            product_ids: All retrieved product IDs, in rank order.
            presented_ids: Product IDs the answer actually recommends, in the
                order presented. Follow-up positions resolve against these;
                falls back to ``product_ids`` when empty.
        """
        self.active_search_text = search_text
        self.last_result_ids = list(presented_ids or product_ids)
        self.focused_product_id = None
        self.add_shown_products(product_ids)

    def add_turn(self, role: str, content: str, **extra: Any) -> None:
        """Append a conversation turn, enforcing the sliding window limit.

        Args:
            role: Message role ('user', 'assistant', 'system').
            content: Message text content.
            **extra: Additional metadata (e.g. parsed_query, intent).
        """
        entry: dict[str, Any] = {"role": role, "content": content}
        entry.update(extra)
        self.conversation_history.append(entry)

        # Enforce sliding window — keep the most recent turns
        if len(self.conversation_history) > self.max_history_turns:
            excess = len(self.conversation_history) - self.max_history_turns
            self.conversation_history = self.conversation_history[excess:]

    def add_shown_products(self, product_ids: list[int]) -> None:
        """Record product IDs that have been shown to the user.

        Args:
            product_ids: List of product IDs shown in the current response.
        """
        for pid in product_ids:
            if pid not in self.shown_product_ids:
                self.shown_product_ids.append(pid)

    def merge_filters(
        self,
        parsed_query: ParsedQuery,
        resolved_fields: Optional[dict[str, Any]] = None,
    ) -> MetadataFilter:
        """Merge new constraints from a parsed query onto the active filters.

        For REFINE_SEARCH: new non-None values override existing ones; product
        types carry over unless new ones are given; exclusions accumulate; a
        price's currency travels with the price.
        For PRODUCT_SEARCH: replaces all filters with fresh constraints.

        Args:
            parsed_query: The newly parsed query with constraints.
            resolved_fields: MetadataFilter fields already resolved against the
                database (``ConstraintResolver``). Defaults to the parsed fields.

        Returns:
            The updated MetadataFilter (also stored in self.active_filters).
        """
        new_constraints = (
            resolved_fields if resolved_fields is not None else parsed_query.to_metadata_filter_dict()
        )

        if parsed_query.intent == UserIntent.REFINE_SEARCH:
            # Merge: only override fields that the user explicitly set
            current = self.active_filters
            new_price = "min_price" in new_constraints or "max_price" in new_constraints
            merged = MetadataFilter(
                category=new_constraints.get("category", current.category),
                brand=new_constraints.get("brand", current.brand),
                min_price=new_constraints.get("min_price", current.min_price),
                max_price=new_constraints.get("max_price", current.max_price),
                min_rating=new_constraints.get("min_rating", current.min_rating),
                category_id=current.category_id,
                brand_id=current.brand_id,
                product_types=tuple(new_constraints.get("product_types") or ()) or current.product_types,
                colours=tuple(new_constraints.get("colours") or ()) or current.colours,
                materials=tuple(new_constraints.get("materials") or ()) or current.materials,
                occasions=tuple(new_constraints.get("occasions") or ()) or current.occasions,
                excluded_brands=tuple(dict.fromkeys(
                    [*current.excluded_brands, *new_constraints.get("excluded_brands", ())])),
                excluded_terms=tuple(dict.fromkeys(
                    [*current.excluded_terms, *new_constraints.get("excluded_terms", ())])),
                price_currency=new_constraints.get("price_currency") if new_price else current.price_currency,
            )
            self.active_filters = merged
        else:
            # Fresh search: replace all filters
            self.active_filters = MetadataFilter(**{
                k: (tuple(v) if isinstance(v, list) else v) for k, v in new_constraints.items()
            })

        return self.active_filters

    FILTER_NAMES = ("category", "brand", "min_price", "max_price", "min_rating")

    def clear_filter(self, name: str) -> MetadataFilter:
        """Remove one active filter for this session (Phase 9; in memory only).

        Args:
            name: One of ``FILTER_NAMES``.

        Returns:
            The updated filters.
        """
        if name not in self.FILTER_NAMES:
            raise ValueError(f"Unknown filter '{name}'")
        updates = {name: None}
        if name == "category":
            updates["category_id"] = None
        if name == "brand":
            updates["brand_id"] = None
        self.active_filters = replace(self.active_filters, **updates)
        return self.active_filters

    def get_recent_history_for_prompt(self, max_turns: int = 6) -> list[dict[str, str]]:
        """Get recent conversation history formatted for LLM prompt injection.

        Args:
            max_turns: Maximum number of recent turns to include.

        Returns:
            List of message dicts with 'role' and 'content' only.
        """
        recent = self.conversation_history[-max_turns:]
        return [{"role": t["role"], "content": t["content"]} for t in recent]

    def get_last_product_id(self) -> Optional[int]:
        """Get the most recently shown product ID for PRODUCT_QUESTION context.

        Returns:
            Last product ID shown, or None if no products have been shown.
        """
        if self.shown_product_ids:
            return self.shown_product_ids[-1]
        return None

    def to_dict(self) -> dict[str, Any]:
        """Serialize session state to a dictionary."""
        return {
            "session_id": self.session_id,
            "active_filters": {
                "category": self.active_filters.category,
                "brand": self.active_filters.brand,
                "min_price": self.active_filters.min_price,
                "max_price": self.active_filters.max_price,
                "min_rating": self.active_filters.min_rating,
            },
            "shown_product_ids": self.shown_product_ids,
            "conversation_turn_count": len(self.conversation_history),
            "last_intent": self.last_intent.value if self.last_intent else None,
            "created_at": self.created_at.isoformat(),
        }

    def clear(self) -> None:
        """Reset session state to initial values."""
        self.active_filters = MetadataFilter()
        self.shown_product_ids = []
        self.conversation_history = []
        self.last_intent = None
        self.last_parsed_query = None
        self.last_result_ids = []
        self.focused_product_id = None
        self.active_search_text = ""
        self.last_search = None
        self.summary_cache = {}


class SessionManager:
    """In-memory session store for active conversations.

    Sessions are ephemeral and do not survive server restarts.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    def get_or_create(self, session_id: Optional[str] = None) -> SessionState:
        """Get an existing session or create a new one.

        Args:
            session_id: Optional session ID. If None or not found, creates new.

        Returns:
            The SessionState for the given or new session.
        """
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]

        new_id = session_id or str(uuid.uuid4())
        session = SessionState(session_id=new_id)
        self._sessions[new_id] = session
        logger.info("session_created", session_id=new_id)
        return session

    def get(self, session_id: str) -> Optional[SessionState]:
        """Get a session by ID, or None if not found.

        Args:
            session_id: The session identifier.

        Returns:
            SessionState or None.
        """
        return self._sessions.get(session_id)

    def delete(self, session_id: str) -> bool:
        """Delete a session.

        Args:
            session_id: The session identifier.

        Returns:
            True if the session existed and was deleted.
        """
        if session_id in self._sessions:
            del self._sessions[session_id]
            logger.info("session_deleted", session_id=session_id)
            return True
        return False

    def list_sessions(self) -> list[str]:
        """Return all active session IDs."""
        return list(self._sessions.keys())

    @property
    def count(self) -> int:
        """Number of active sessions."""
        return len(self._sessions)
