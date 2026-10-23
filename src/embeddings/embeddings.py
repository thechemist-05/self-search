# src/embeddings/embeddings.py
"""Utility functions for obtaining text embeddings.

The default embedder is the deterministic offline 64-d feature hashing that the
current FAISS indexes were built with (see each index's manifest.json). Ollama
embeddings are used only when explicitly selected with
``SHELFSENSE_EMBEDDING_BACKEND=ollama``: they have a different dimension, so
switching requires an Ollama *embedding* model, rebuilt indexes and re-checked
evidence-validation thresholds. Ollama being reachable is not enough, because the
local Ollama install may serve only a chat model.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from typing import List

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
_EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "llama3")
_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "10"))  # seconds
# "offline" (default, matches the current indexes) or "ollama" (explicit opt-in).
_EMBEDDING_BACKEND = os.getenv("SHELFSENSE_EMBEDDING_BACKEND", "offline").strip().lower()


def ollama_embeddings_selected() -> bool:
    """True only when Ollama embeddings were explicitly selected and the daemon is reachable."""
    return _EMBEDDING_BACKEND == "ollama" and check_ollama_available()

_OLLAMA_AVAILABLE: bool | None = None


def check_ollama_available() -> bool:
    """Check if the local Ollama daemon is reachable with a short timeout."""
    global _OLLAMA_AVAILABLE
    if _OLLAMA_AVAILABLE is not None:
        return _OLLAMA_AVAILABLE
    try:
        resp = requests.get(f"{_OLLAMA_URL}/api/tags", timeout=0.3)
        _OLLAMA_AVAILABLE = resp.status_code == 200
    except Exception:
        _OLLAMA_AVAILABLE = False
    return _OLLAMA_AVAILABLE


def reset_ollama_status() -> None:
    """Reset cached Ollama availability status (useful in tests)."""
    global _OLLAMA_AVAILABLE
    _OLLAMA_AVAILABLE = None


def _post(endpoint: str, payload: dict) -> dict:
    """Internal helper to POST JSON to the Ollama HTTP API.

    Raises
    ------
    RuntimeError
        If the request fails or the response cannot be decoded.
    """
    url = f"{_OLLAMA_URL}{endpoint}"
    try:
        response = requests.post(url, json=payload, timeout=_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise RuntimeError(f"Ollama request to {endpoint} failed: {exc}") from exc


def get_embedding(text: str) -> List[float]:
    """Return a dense vector embedding for *text* via Ollama.

    The function calls the Ollama ``/api/embeddings`` endpoint. The returned
    JSON contains an ``embedding`` field which is a list of floats.

    Parameters
    ----------
    text:
        The raw text to embed.

    Returns
    -------
    List[float]
        The embedding vector.
    """
    payload = {"model": _EMBEDDING_MODEL, "prompt": text}
    result = _post("/api/embeddings", payload)
    if "embedding" not in result:
        raise RuntimeError("Unexpected Ollama response – missing 'embedding' key")
    return result["embedding"]


def deterministic_embedding(text: str, dim: int = 64) -> List[float]:
    """Generate a deterministic, offline feature-hashed embedding vector.

    Extracts word unigrams and bigrams, hashes them into `dim` buckets with sign
    hashing, and normalizes the vector to unit L2 norm. This preserves lexical
    similarity and word overlap without requiring any network calls or external models.

    Parameters
    ----------
    text:
        The input text string.
    dim:
        Dimensionality of the output embedding vector (default: 64).

    Returns
    -------
    List[float]
        Unit L2-normalized float vector of length `dim`.
    """
    vec = [0.0] * dim
    if not text or not text.strip():
        vec[0] = 1.0
        return vec

    # Clean and tokenize words
    words = re.findall(r"\b[A-Za-z0-9_\-]+\b", text.lower())
    if not words:
        vec[0] = 1.0
        return vec

    features: list[str] = list(words)
    # Add bigrams for local context sensitivity
    for i in range(len(words) - 1):
        features.append(f"{words[i]}_{words[i+1]}")

    for feat in features:
        # 32-bit hash
        h = int(hashlib.md5(feat.encode("utf-8")).hexdigest()[:8], 16)
        bucket = h % dim
        sign = 1.0 if (h & 0x10000000) else -1.0
        vec[bucket] += sign

    # Compute L2 norm and normalize
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 1e-9:
        vec = [float(v / norm) for v in vec]
    else:
        vec[0] = 1.0

    return vec


def get_local_or_remote_embedding(text: str, dim: int = 64) -> List[float]:
    """Embedding with the configured backend (offline 64-d hashing by default).

    With ``SHELFSENSE_EMBEDDING_BACKEND=ollama`` it calls Ollama and falls back to
    deterministic_embedding(text, dim=dim) if that fails.
    """
    if ollama_embeddings_selected():
        try:
            return get_embedding(text)
        except Exception:
            pass
    return deterministic_embedding(text, dim=dim)


def embed_batch(texts: List[str]) -> List[List[float]]:
    """Obtain embeddings for a batch of texts using get_embedding."""
    return [get_embedding(t) for t in texts]


def embed_batch_offline(texts: List[str], dim: int = 64) -> List[List[float]]:
    """Obtain deterministic embeddings for a batch of texts without network calls."""
    return [deterministic_embedding(t, dim=dim) for t in texts]
