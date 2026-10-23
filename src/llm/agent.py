# src/llm/agent.py
"""LLM agent that answers user queries using retrieved evidence.

The agent performs three steps:

1. **Retrieve** the top‑k nearest neighbours for the query using the
   ``src.retrieval.retrieval`` module.
2. **Construct** a prompt that injects the retrieved evidence (product or
   review snippets) into a system‑level instruction.
3. **Call** the local Ollama chat endpoint (model name configurable via the
   ``LLM_MODEL`` environment variable) and return the generated answer.

The implementation is deliberately lightweight and can be mocked in unit
tests – the heavy work (embedding generation and vector search) is delegated to
the existing modules.
"""

import os
import json
from typing import List, Tuple

import requests

from src.retrieval.retrieval import retrieve
from src.embeddings.embeddings import get_embedding

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
_LLM_MODEL = os.getenv("LLM_MODEL", "llama3")
_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "20"))  # seconds


def _post_chat(messages: List[dict]) -> str:
    """Send a chat request to Ollama and return the assistant's reply.

    Parameters
    ----------
    messages:
        List of message dicts, each with ``role`` ("system", "user", "assistant")
        and ``content``.
    """
    payload = {
        "model": _LLM_MODEL,
        "messages": messages,
        "stream": False,
    }
    try:
        resp = requests.post(f"{_OLLAMA_URL}/api/chat", json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        # Ollama returns a list of messages; the assistant's content is in the last entry.
        if "message" in data and "content" in data["message"]:
            return data["message"]["content"].strip()
        # Fallback to older format
        if "response" in data:
            return data["response"].strip()
        raise RuntimeError("Unexpected Ollama chat response format")
    except Exception as exc:
        raise RuntimeError(f"Ollama chat request failed: {exc}") from exc


def _format_evidence(evidence: List[Tuple[str, int, float]]) -> str:
    """Create a readable string from retrieved evidence.

    The ``evidence`` list contains ``(entity_type, entity_id, distance)``.
    For now we only include the IDs – callers can look them up in the DB if
    they need full details.
    """
    lines = []
    for idx, (etype, eid, dist) in enumerate(evidence, start=1):
        lines.append(f"{idx}. {etype.title()} ID {eid} (distance {dist:.2f})")
    return "\n".join(lines) if lines else "(no relevant items found)"


def answer_query(query: str, k: int = 5) -> str:
    """Answer a user *query* using retrieved context.

    The function:

    1. Retrieves the top‑k closest items from the vector store.
    2. Embeds the query (via ``get_embedding``) – the embedding step is
       delegated to the same utility used by the retrieval layer, ensuring a
       consistent vector space.
    3. Sends a system prompt containing the evidence list and asks the LLM to
       produce a concise answer.
    """
    # Step 1 – retrieve nearest neighbours.
    evidence = retrieve(query, k=k)
    evidence_str = _format_evidence(evidence)

    # Step 2 – build the chat messages.
    system_prompt = (
        "You are an intelligent product‑information assistant. Use the "
        "provided evidence list to answer the user's question. If the evidence "
        "does not contain sufficient information, politely indicate that."
    )
    user_prompt = f"Question: {query}\n\nEvidence:\n{evidence_str}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # Step 3 – call Ollama chat.
    return _post_chat(messages)

# End of file
