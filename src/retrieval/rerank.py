"""
Optional Cross-Encoder reranking for Self Search candidates (Phase 4).

Reranks candidate products using a cross-encoder model
(default: 'cross-encoder/ms-marco-MiniLM-L-6-v2').
Configurable and optional; falls back gracefully in offline or resource-constrained environments.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("shelfsense.retrieval.rerank")


class CrossEncoderReranker:
    """Reranks candidate products using a cross-encoder model."""

    def __init__(
        self,
        model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
        enabled: Optional[bool] = None,
    ) -> None:
        self.model_name = model_name
        if enabled is None:
            self.enabled = os.getenv("SHELFSENSE_ENABLE_CROSS_ENCODER", "0").lower() in ("1", "true", "yes")
        else:
            self.enabled = enabled
        self._model = None
        self._load_attempted = False

    def _get_model(self):
        """Lazy loader for CrossEncoder."""
        if not self.enabled:
            return None

        if not self._load_attempted:
            self._load_attempted = True
            try:
                from sentence_transformers import CrossEncoder
                self._model = CrossEncoder(self.model_name)
                logger.info(f"Loaded CrossEncoder model: {self.model_name}")
            except Exception as e:
                logger.warning(
                    f"Could not load CrossEncoder model '{self.model_name}': {e}. "
                    "Reranking will maintain hybrid fusion ranking."
                )
                self._model = None
        return self._model

    def rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Rerank candidates based on cross-encoder similarity with query.

        Args:
            query: User search query.
            candidates: List of candidate product dicts.
            limit: Maximum number of reranked candidates to return.

        Returns:
            Reranked candidate list sorted by 'rerank_score' descending.
        """
        if not candidates or not query:
            return candidates[:limit] if limit else candidates

        model = self._get_model()
        if model is None:
            # Fallback: annotate source as reranked_hybrid and return candidates
            results = []
            for item in (candidates[:limit] if limit else candidates):
                c = dict(item)
                c["retrieval_source"] = "reranked_hybrid"
                results.append(c)
            return results

        # Format pairs: (query, title + description)
        pairs: list[tuple[str, str]] = []
        for c in candidates:
            title = c.get("title") or ""
            desc = c.get("description") or ""
            doc_text = f"{title}. {desc}".strip()
            pairs.append((query, doc_text))

        try:
            scores = model.predict(pairs)
            scored_candidates: list[dict[str, Any]] = []
            for item, score in zip(candidates, scores):
                candidate_copy = dict(item)
                candidate_copy["rerank_score"] = float(score)
                candidate_copy["score"] = float(score)
                candidate_copy["retrieval_source"] = "reranked_hybrid"
                scored_candidates.append(candidate_copy)

            scored_candidates.sort(key=lambda x: x["rerank_score"], reverse=True)
            return scored_candidates[:limit] if limit else scored_candidates
        except Exception as e:
            logger.warning(f"Reranking prediction failed: {e}. Returning original candidates.")
            return candidates[:limit] if limit else candidates
