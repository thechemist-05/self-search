"""
LLM Provider interface and abstractions for Self Search (Phase 5).

Provides a uniform interface for local open-weight language models (e.g. Ollama)
and a mock provider for deterministic, offline testing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import time
from typing import Any, Callable, Optional


@dataclass
class LLMResponse:
    """Standard container for language model generation output."""

    content: str
    model: str
    latency_ms: float
    raw_response: dict[str, Any] = field(default_factory=dict)


class LLMProvider(ABC):
    """Abstract base class for all LLM inference providers."""

    @abstractmethod
    def generate(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a sequence of chat messages to the model and return the reply.

        Args:
            messages: List of message dicts, each with 'role' ('system', 'user', 'assistant')
                      and 'content'.
            temperature: Sampling temperature (default 0.2 for deterministic formatting).
            **kwargs: Extra provider-specific parameters.

        Returns:
            LLMResponse containing generated text and latency metrics.
        """
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check whether the provider runtime is active and reachable."""
        pass


class MockLLMProvider(LLMProvider):
    """Mock LLM provider for fast, deterministic unit testing."""

    def __init__(
        self,
        responses: Optional[list[str]] = None,
        default_response: str = "{}",
        handler: Optional[Callable[[list[dict[str, str]]], str]] = None,
        available: bool = True,
    ) -> None:
        self.responses = list(responses) if responses else []
        self.default_response = default_response
        self.handler = handler
        self._available = available
        self.history: list[list[dict[str, str]]] = []
        self.call_count = 0

    def is_available(self) -> bool:
        return self._available

    def generate(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> LLMResponse:
        start_time = time.perf_counter()
        self.history.append(messages)
        self.call_count += 1

        if self.handler is not None:
            content = self.handler(messages)
        elif self.responses:
            content = self.responses.pop(0)
        else:
            content = self.default_response

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        return LLMResponse(
            content=content,
            model="mock-llm",
            latency_ms=round(latency_ms, 2),
            raw_response={"mock": True},
        )
