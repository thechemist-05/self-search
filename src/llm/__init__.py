"""LLM provider abstraction, prompts, and response schemas (Phase 5)."""

from src.llm.ollama import (
    OllamaConnectionError,
    OllamaError,
    OllamaProvider,
    OllamaTimeoutError,
)
from src.llm.prompts import SYSTEM_PROMPT, build_chat_messages
from src.llm.provider import LLMProvider, LLMResponse, MockLLMProvider
from src.llm.schemas import (
    EvidenceClaim,
    ProductRecommendation,
    StructuredAnswer,
)
from src.llm.service import LLMService, extract_json_from_text

__all__ = [
    "EvidenceClaim",
    "LLMProvider",
    "LLMResponse",
    "LLMService",
    "MockLLMProvider",
    "OllamaConnectionError",
    "OllamaError",
    "OllamaProvider",
    "OllamaTimeoutError",
    "ProductRecommendation",
    "SYSTEM_PROMPT",
    "StructuredAnswer",
    "build_chat_messages",
    "extract_json_from_text",
]
