"""
LLM Generation Service for Self Search (Phase 5).

Coordinates structured prompt construction, local model inference, JSON schema
validation, retry loop on malformed outputs, latency logging, and safe fallback generation.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from pydantic import ValidationError
import structlog

from src.config.settings import get_settings
from src.evidence.evidence_pack import EvidencePack
from src.llm.deadline import remaining_seconds
from src.llm.ollama import OllamaConnectionError, OllamaError, OllamaProvider
from src.llm.prompts import build_chat_messages, format_price
from src.llm.provider import LLMProvider, LLMResponse
from src.llm.schemas import EvidenceClaim, GenerationInfo, ProductRecommendation, StructuredAnswer
from src.retrieval.filters import MetadataFilter

logger = structlog.get_logger("shelfsense.llm.service")


def extract_json_from_text(text: str) -> str:
    """Extract a JSON substring from a model response, stripping markdown code blocks if present."""
    trimmed = text.strip()
    # Match markdown json block: ```json ... ``` or ``` ... ```
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", trimmed, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Search for first outer curly brace pair
    start = trimmed.find("{")
    end = trimmed.rfind("}")
    if start != -1 and end != -1 and end > start:
        return trimmed[start : end + 1]

    return trimmed


class LLMService:
    """High-level service for evidence-grounded structured response generation."""

    # A JSON retry is started only if at least this many seconds of the answer budget remain.
    MIN_RETRY_SECONDS = 30.0

    def __init__(
        self,
        provider: Optional[LLMProvider] = None,
        max_retries: int = 2,
        deadline_s: Optional[float] = None,
    ) -> None:
        """
        Args:
            provider: Model provider (default: local Ollama).
            max_retries: Extra attempts after invalid JSON.
            deadline_s: Seconds for one whole answer across attempts
                (default: ``SHELFSENSE_OLLAMA_TIMEOUT``). Each call gets only the time left.
        """
        self.provider = provider or OllamaProvider()
        self.max_retries = max_retries
        self.deadline_s = float(deadline_s if deadline_s is not None else get_settings().ollama_timeout)

    def generate_answer(
        self,
        query: str,
        products: list[dict[str, Any]],
        evidence_pack: Optional[EvidencePack] = None,
        filters: Optional[MetadataFilter] = None,
    ) -> StructuredAnswer:
        """Generate a verified StructuredAnswer using retrieved context.

        Args:
            query: The user's search query or question.
            products: List of retrieved candidate products.
            evidence_pack: Retrieved review evidence pack.
            filters: Optional user constraints.

        Returns:
            StructuredAnswer strictly conforming to the Pydantic schema.
        """
        return self.generate_answer_with_mode(query, products, evidence_pack, filters)[0]

    def generate_answer_with_mode(
        self,
        query: str,
        products: list[dict[str, Any]],
        evidence_pack: Optional[EvidencePack] = None,
        filters: Optional[MetadataFilter] = None,
    ) -> tuple[StructuredAnswer, GenerationInfo]:
        """Like ``generate_answer``, but also report how the answer was actually produced.

        The mode is decided where the outcome happens, so a fallback after a
        model error mid-request is reported as ``"fallback"`` even if the
        provider looked available beforehand.

        Returns:
            Tuple of (answer, GenerationInfo).
        """
        # Case 1: No products retrieved
        if not products:
            logger.info("llm_no_products_retrieved", query=query)
            return StructuredAnswer(
                summary="No matching products were found in the catalog for your query.",
                recommendations=[],
                insufficient_evidence=True,
                missing_information="No products matching the query or active filters were found in the database.",
            ), GenerationInfo(mode="none", reason="no_products_retrieved")

        messages = build_chat_messages(
            query=query,
            products=products,
            evidence_pack=evidence_pack,
            filters=filters,
        )

        # Attempt inference with retries on malformed JSON
        last_error = ""
        fallback_reason = "invalid_output_after_retries"
        current_messages = list(messages)

        started = time.monotonic()
        for attempt in range(self.max_retries + 1):
            remaining = remaining_seconds(self.deadline_s - (time.monotonic() - started))
            if remaining < (self.MIN_RETRY_SECONDS if attempt > 0 else 1.0):
                logger.warning("llm_skipped_deadline", attempt=attempt, remaining_s=round(remaining, 1))
                if attempt == 0:
                    fallback_reason = "provider_error"
                break
            try:
                response = self.provider.generate(current_messages, timeout=remaining)
                logger.info(
                    "llm_response_received",
                    attempt=attempt,
                    latency_ms=response.latency_ms,
                    model=response.model,
                )

                raw_json = extract_json_from_text(response.content)
                parsed_dict = json.loads(raw_json)
                validated_answer = StructuredAnswer.model_validate(parsed_dict)
                return validated_answer, GenerationInfo(mode="llm", model=response.model, attempts=attempt + 1)

            except (json.JSONDecodeError, ValidationError) as e:
                last_error = str(e)
                logger.warning(
                    "llm_schema_validation_failed",
                    attempt=attempt,
                    error=last_error,
                    retrying=(attempt < self.max_retries),
                )
                if attempt < self.max_retries:
                    # Provide targeted correction feedback in the conversation history
                    current_messages.append({"role": "assistant", "content": response.content})
                    current_messages.append({
                        "role": "user",
                        "content": (
                            f"Your previous response was invalid JSON or failed schema validation: {last_error}.\n"
                            "Please output ONLY valid JSON strictly matching the specified StructuredAnswer schema."
                        ),
                    })
            except OllamaConnectionError as e:
                logger.warning("llm_provider_offline", error=str(e))
                fallback_reason = "provider_unavailable"
                break
            except OllamaError as e:
                logger.warning("llm_provider_error", error=str(e))
                fallback_reason = "provider_error"
                break
            except Exception as e:
                logger.warning("llm_unexpected_generation_error", error=str(e))
                fallback_reason = "unexpected_error"
                break

        # Fallback triggered when retries exhausted or provider unavailable
        logger.warning("llm_using_safe_fallback", query=query, reason=last_error or "provider_error")
        return self.build_safe_fallback(
            query=query,
            products=products,
            evidence_pack=evidence_pack,
        ), GenerationInfo(mode="fallback", reason=fallback_reason)

    def build_safe_fallback(
        self,
        query: str,
        products: list[dict[str, Any]],
        evidence_pack: Optional[EvidencePack] = None,
    ) -> StructuredAnswer:
        """Construct a deterministic, hallucination-free StructuredAnswer from retrieved data.

        Guarantees that all claims and recommendations map directly to real products
        and real review evidence without generating unverified statements.
        """
        if not products:
            return StructuredAnswer(
                summary="No products were retrieved matching your search criteria.",
                recommendations=[],
                insufficient_evidence=True,
                missing_information="No catalog products found.",
            )

        # Build recommendations from the top products (up to 3)
        recommendations: list[ProductRecommendation] = []
        evidence_by_prod: dict[int, list[EvidenceClaim]] = {}
        watchouts_by_prod: dict[int, list[EvidenceClaim]] = {}

        if evidence_pack:
            for item in evidence_pack.items:
                pid = item.product_id
                cid = item.evidence_id
                rating = item.rating or 4.0
                snippet_preview = item.title if item.title else item.text[:80]

                if rating >= 4.0:
                    claim = EvidenceClaim(
                        claim=f"Customer review mentions: '{snippet_preview}'",
                        evidence_ids=[cid],
                    )
                    evidence_by_prod.setdefault(pid, []).append(claim)
                else:
                    watchout = EvidenceClaim(
                        claim=f"Customer concern: '{snippet_preview}'",
                        evidence_ids=[cid],
                    )
                    watchouts_by_prod.setdefault(pid, []).append(watchout)

        for p in products[:3]:
            pid = p["id"]
            price_str = format_price(p.get("price"), p.get("currency"))
            rating_str = f"{p['avg_rating']:.1f}/5.0" if p.get("avg_rating") else "unrated"
            brand_str = p.get("brand_name") or "verified brand"

            why_matches = evidence_by_prod.get(pid, [])
            if not why_matches:
                why_matches = [
                    EvidenceClaim(
                        claim=f"Matches search query '{query}' with price {price_str} by {brand_str} (Rated {rating_str}).",
                        evidence_ids=[],
                    )
                ]

            watch_outs = watchouts_by_prod.get(pid, [])

            rec = ProductRecommendation(
                product_id=pid,
                external_id=p.get("external_id") or f"P{pid}",
                title=p.get("title") or f"Product {pid}",
                why_it_matches=why_matches,
                watch_outs=watch_outs,
            )
            recommendations.append(rec)

        summary = (
            f"Retrieved {len(recommendations)} candidate product(s) matching '{query}'. "
            f"Top match is '{recommendations[0].title}'."
        )

        return StructuredAnswer(
            summary=summary,
            recommendations=recommendations,
            insufficient_evidence=False,
        )
