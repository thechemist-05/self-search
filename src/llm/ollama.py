"""
Ollama inference provider for local LLM generation in Self Search.

Communicates with the local Ollama HTTP API (e.g. http://localhost:11434).
"""

from __future__ import annotations

import time
from typing import Any, Optional

import requests
import structlog

from src.config.settings import get_settings
from src.llm.provider import LLMProvider, LLMResponse

logger = structlog.get_logger("shelfsense.llm.ollama")


class OllamaError(RuntimeError):
    """Base exception for Ollama provider errors."""
    pass


class OllamaConnectionError(OllamaError):
    """Raised when the local Ollama daemon cannot be reached."""
    pass


class OllamaTimeoutError(OllamaError):
    """Raised when an Ollama request times out."""
    pass


class OllamaProvider(LLMProvider):
    """Local LLM inference provider using Ollama's HTTP API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or settings.ollama_model
        self.timeout = timeout if timeout is not None else settings.ollama_timeout

    def is_available(self) -> bool:
        """Check if the local Ollama server is running and reachable."""
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=0.5)
            return resp.status_code == 200
        except Exception:
            return False

    def generate(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        format_json: bool = True,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send chat messages to Ollama and return the generated assistant response.

        Args:
            messages: List of chat messages with 'role' and 'content'.
            temperature: Sampling temperature.
            format_json: If True, passes format='json' to Ollama to enforce valid JSON grammar.
            timeout: Seconds for this request (default: the provider's timeout); never longer.
            **kwargs: Extra parameters passed to the Ollama payload.

        Returns:
            LLMResponse containing the assistant's text and execution latency.
        """
        settings = get_settings()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": settings.ollama_num_predict},
            "keep_alive": settings.ollama_keep_alive,
        }
        request_timeout = self.timeout if timeout is None else max(1.0, min(float(timeout), self.timeout))
        if format_json:
            payload["format"] = "json"

        for k, v in kwargs.items():
            payload[k] = v

        url = f"{self.base_url}/api/chat"
        logger.info("ollama_request_start", model=self.model, url=url, messages_count=len(messages))

        start_time = time.perf_counter()
        try:
            resp = requests.post(url, json=payload, timeout=request_timeout)
            resp.raise_for_status()
            data = resp.json()
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout) as e:
            logger.error("ollama_timeout", error=str(e), timeout=request_timeout)
            raise OllamaTimeoutError(f"Ollama request to {url} timed out after {request_timeout}s") from e
        except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
            logger.error("ollama_connection_error", error=str(e))
            raise OllamaConnectionError(f"Failed to connect to Ollama daemon at {self.base_url}") from e
        except Exception as e:
            logger.error("ollama_unexpected_error", error=str(e))
            raise OllamaError(f"Ollama request failed: {e}") from e

        latency_ms = (time.perf_counter() - start_time) * 1000.0

        content = ""
        if "message" in data and "content" in data["message"]:
            content = data["message"]["content"].strip()
        elif "response" in data:
            content = data["response"].strip()
        else:
            raise OllamaError(f"Unexpected Ollama response structure: {data}")

        logger.info("ollama_request_success", model=self.model, latency_ms=round(latency_ms, 2))

        return LLMResponse(
            content=content,
            model=self.model,
            latency_ms=round(latency_ms, 2),
            raw_response=data,
        )
