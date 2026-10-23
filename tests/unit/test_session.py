"""
Unit tests for Self Search Phase 7 Session State Management.

Tests session creation, turn recording with sliding window, shown products tracking,
filter accumulation/merging across refinements, and SessionManager operations.
"""

from __future__ import annotations

import pytest

from src.agent.schemas import ParsedQuery, UserIntent
from src.agent.session import SessionManager, SessionState
from src.retrieval.filters import MetadataFilter


class TestSessionState:
    """Tests for SessionState behaviour."""

    def test_session_state_defaults(self) -> None:
        state = SessionState(session_id="test-123")
        assert state.session_id == "test-123"
        assert state.active_filters.is_empty()
        assert state.shown_product_ids == []
        assert state.conversation_history == []
        assert state.last_intent is None
        assert state.last_parsed_query is None

    def test_add_turn_and_sliding_window(self) -> None:
        state = SessionState(session_id="test-123", max_history_turns=4)
        for i in range(6):
            state.add_turn(role="user", content=f"msg {i}")

        assert len(state.conversation_history) == 4
        # Should keep the most recent turns (2, 3, 4, 5)
        assert state.conversation_history[0]["content"] == "msg 2"
        assert state.conversation_history[-1]["content"] == "msg 5"

    def test_add_shown_products_deduplicates(self) -> None:
        state = SessionState(session_id="test-123")
        state.add_shown_products([1, 2, 3])
        state.add_shown_products([2, 3, 4, 5])
        assert state.shown_product_ids == [1, 2, 3, 4, 5]

    def test_get_last_product_id(self) -> None:
        state = SessionState(session_id="test-123")
        assert state.get_last_product_id() is None
        state.add_shown_products([10, 20])
        assert state.get_last_product_id() == 20

    def test_merge_filters_product_search_replaces_all(self) -> None:
        state = SessionState(session_id="test-123")
        state.active_filters = MetadataFilter(brand="Sony", max_price=100.0)

        # Fresh search with only category
        query = ParsedQuery(
            intent=UserIntent.PRODUCT_SEARCH,
            raw_query="laptops",
            category="laptops",
            brand=None,
            max_price=None,
        )
        updated = state.merge_filters(query)
        assert updated.category == "laptops"
        assert updated.brand is None
        assert updated.max_price is None

    def test_merge_filters_refine_search_accumulates(self) -> None:
        state = SessionState(session_id="test-123")
        state.active_filters = MetadataFilter(category="headphones", max_price=100.0)

        # Refinement specifying brand=Sony
        refine_query = ParsedQuery(
            intent=UserIntent.REFINE_SEARCH,
            raw_query="only Sony",
            brand="Sony",
        )
        updated = state.merge_filters(refine_query)
        assert updated.category == "headphones"  # Preserved!
        assert updated.max_price == 100.0       # Preserved!
        assert updated.brand == "Sony"          # Added!

    def test_get_recent_history_for_prompt(self) -> None:
        state = SessionState(session_id="test-123")
        for i in range(10):
            role = "user" if i % 2 == 0 else "assistant"
            state.add_turn(role=role, content=f"turn {i}")

        prompt_history = state.get_recent_history_for_prompt(max_turns=3)
        assert len(prompt_history) == 3
        assert prompt_history[-1]["content"] == "turn 9"

    def test_clear_resets_session(self) -> None:
        state = SessionState(session_id="test-123")
        state.active_filters = MetadataFilter(brand="Sony")
        state.add_shown_products([1, 2])
        state.add_turn("user", "hi")
        state.last_intent = UserIntent.PRODUCT_SEARCH

        state.clear()
        assert state.active_filters.is_empty()
        assert state.shown_product_ids == []
        assert state.conversation_history == []
        assert state.last_intent is None

    def test_set_search_results_tracks_ranking_and_resets_focus(self) -> None:
        state = SessionState(session_id="test-123")
        state.set_search_results("headphones", [3, 1, 2])
        state.focused_product_id = 1

        state.set_search_results("laptops", [9, 1])
        assert state.last_result_ids == [9, 1]
        assert state.active_search_text == "laptops"
        assert state.focused_product_id is None
        assert state.shown_product_ids == [3, 1, 2, 9]

    def test_presented_ids_take_precedence_for_follow_ups(self) -> None:
        state = SessionState(session_id="test-123")
        state.set_search_results("earbuds", [1, 2, 3, 4], presented_ids=[3, 1])
        assert state.last_result_ids == [3, 1]
        assert state.shown_product_ids == [1, 2, 3, 4]

    def test_clear_resets_follow_up_state(self) -> None:
        state = SessionState(session_id="test-123")
        state.set_search_results("headphones", [1, 2])
        state.focused_product_id = 2
        state.clear()
        assert state.last_result_ids == []
        assert state.focused_product_id is None
        assert state.active_search_text == ""

    def test_to_dict_serialization(self) -> None:
        state = SessionState(session_id="test-456")
        state.active_filters = MetadataFilter(category="shoes", min_price=50.0)
        state.add_shown_products([42])
        state.last_intent = UserIntent.PRODUCT_SEARCH

        d = state.to_dict()
        assert d["session_id"] == "test-456"
        assert d["active_filters"]["category"] == "shoes"
        assert d["active_filters"]["min_price"] == 50.0
        assert d["shown_product_ids"] == [42]
        assert d["last_intent"] == "PRODUCT_SEARCH"


class TestSessionManager:
    """Tests for SessionManager operations."""

    def test_get_or_create_new_session(self) -> None:
        manager = SessionManager()
        s1 = manager.get_or_create()
        assert s1.session_id is not None
        assert manager.count == 1

    def test_get_or_create_with_specific_id(self) -> None:
        manager = SessionManager()
        s1 = manager.get_or_create("custom-id-1")
        assert s1.session_id == "custom-id-1"

        # Calling again with same ID returns existing session
        s2 = manager.get_or_create("custom-id-1")
        assert s1 is s2

    def test_get_existing_and_nonexistent(self) -> None:
        manager = SessionManager()
        manager.get_or_create("sess-1")
        assert manager.get("sess-1") is not None
        assert manager.get("nonexistent") is None

    def test_delete_session(self) -> None:
        manager = SessionManager()
        manager.get_or_create("sess-del")
        assert manager.delete("sess-del") is True
        assert manager.delete("sess-del") is False
        assert manager.get("sess-del") is None

    def test_list_sessions(self) -> None:
        manager = SessionManager()
        manager.get_or_create("s1")
        manager.get_or_create("s2")
        assert set(manager.list_sessions()) == {"s1", "s2"}
