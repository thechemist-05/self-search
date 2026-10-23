# src/knowledge/embeddings.py
"""Utility to generate text embeddings using Ollama (or fallback).

The function ``embed_texts`` accepts a list of strings and returns a ``np.ndarray``
with shape ``(len(texts), dim)`` where ``dim`` is the embedding dimension of the
model.

If the local Ollama server is unavailable, the implementation falls back to
``sentence_transformers`` (the *all-MiniLM-L6-v2* model) which is lightweight and
does not require a running server.
"""

from __future__ import annotations

import json
import os
from typing import List

import numpy as np
import requests

# Optional fallback – import only when needed to avoid heavy import at start.
_fallback_model = None  # lazy‑load variable


def _get_ollama_endpoint() -> str:
    """Return the Ollama embeddings endpoint.

    Ollama runs on ``http://localhost:11434`` by default. The environment variable
    ``OLLAMA_HOST`` can override the host (e.g., ``http://127.0.0.1:11434``).
    """
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    return f"{host.rstrip('/')}/api/embeddings"


def _call_ollama(texts: List[str]) -> np.ndarray:
    """Call Ollama's ``/api/embeddings`` endpoint.

    The request payload follows Ollama's API specification:
    ``{"model": "<model>", "prompt": ["text1", "text2", ...]}``

    The response contains ``embedding`` – a list of floats for each input.
    If the request fails we raise ``RuntimeError`` so the caller can fallback.
    """
    url = _get_ollama_endpoint()
    payload = {
        "model": os.getenv("OLLAMA_EMBED_MODEL", "llama3:8b"),
        "prompt": texts,
    }
    resp = requests.post(url, json=payload, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama embedding request failed: {resp.status_code} {resp.text}")
    data = resp.json()
    # ``embeddings`` is a list of lists (one per prompt).
    embeddings = data.get("embeddings")
    if not embeddings:
        raise RuntimeError("Ollama response missing 'embeddings' field")
    return np.asarray(embeddings, dtype=np.float32)


def _fallback_embeddings(texts: List[str]) -> np.ndarray:
    """Generate embeddings using ``sentence_transformers`` as a fallback.

    The heavy import is delayed until needed. The MiniLM model is small (~33 MB)
    and works well for short product/review texts.
    """
    global _fallback_model
    if _fallback_model is None:
        from sentence_transformers import SentenceTransformer
        _fallback_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _fallback_model.encode(texts, convert_to_numpy=True, normalize_embeddings=False).astype(np.float32)


def embed_texts(texts: List[str]) -> np.ndarray:
    """Public API – return embeddings for *texts*.

    The function first attempts to use Ollama. If the server is unreachable or
    returns an error, it silently falls back to ``sentence_transformers``.
    """
    if not texts:
        return np.empty((0, 0), dtype=np.float32)
    try:
        return _call_ollama(texts)
    except Exception as exc:  # pragma: no cover – exercised only when Ollama missing
        # Log the fallback – in production we would use ``structlog`` but imports
        # here would create a circular dependency, so we keep it simple.
        print(f"[embed_texts] Ollama failed ({exc}); using fallback model")
        return _fallback_embeddings(texts)
