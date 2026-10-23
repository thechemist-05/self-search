"""
Agent module for Self Search (Phase 7).

Provides query understanding, intent routing, session state tracking,
and conversational orchestration with evidence validation.
"""

from src.agent.controller import AgentController
from src.agent.intent_router import HandlerResult, IntentRouter
from src.agent.query_parser import QueryParser, parse_query_rule_based
from src.agent.schemas import AgentResponse, ParsedQuery, UserIntent
from src.agent.session import SessionManager, SessionState

__all__ = [
    "AgentController",
    "AgentResponse",
    "HandlerResult",
    "IntentRouter",
    "ParsedQuery",
    "QueryParser",
    "SessionManager",
    "SessionState",
    "UserIntent",
    "parse_query_rule_based",
]
