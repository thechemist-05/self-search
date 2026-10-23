"""Request-scoped model-time budget (one per chat message).

``AgentController.handle_message`` opens a budget of ``SHELFSENSE_OLLAMA_TIMEOUT``
seconds. Every model call in that request (LLM query parsing, answer generation and
its retries) gets only the time that is left, so one message never waits for
sequential model calls longer than the budget. When it runs out, the deterministic
safe fallback is used. Outside a budget (scripts, tests) callers use their own limit.

Context variables are per thread and per request, so concurrent requests never
share a budget.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

_DEADLINE: ContextVar[Optional[float]] = ContextVar("shelfsense_model_deadline", default=None)


@contextmanager
def model_time_budget(seconds: float) -> Iterator[None]:
    """Limit all model calls inside the block to ``seconds`` in total."""
    token = _DEADLINE.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def remaining_seconds(limit: float) -> float:
    """Seconds a model call may take now: ``limit``, or less if the request budget is lower."""
    deadline = _DEADLINE.get()
    if deadline is None:
        return limit
    return min(limit, deadline - time.monotonic())
