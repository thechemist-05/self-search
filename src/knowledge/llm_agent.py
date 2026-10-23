"""LLM agent wrapper for Self Search.

The function ``answer_query`` takes a user query, retrieves the most relevant
product/review evidence via :func:`src.knowledge.retrieval.search`, formats a
prompt, and calls the Ollama chat endpoint to generate a natural‑language answer.

The prompt instructs the model to **only** use the supplied evidence. If no
evidence is found, the model is asked to respond that it does not have enough
information.
"""

from __future__ import annotations

import os
import json
from typing import List, Dict

import requests

from .retrieval import search


def _format_evidence(evidence: List[Dict]) -> str:
    """Create a human‑readable evidence block for the LLM.

    Each entry is turned into a bullet point showing the type and the most
    relevant fields. Example output:

    - **Product**: "Acme Wireless Headphones" – "High‑quality sound with noise cancellation."
    - **Review**: "Great sound, battery lasts long." (product_id=12)
    """
    lines = []
    for item in evidence:
        if item["entity_type"] == "product":
            lines.append(
                f"- **Product**: \"{item.get('title', '')}\" – \"{item.get('description', '')}\""
            )
        elif item["entity_type"] == "review":
            lines.append(
                f"- **Review** (product_id={item.get('product_id')}): \"{item.get('text', '')}\""
            )
    return "\n".join(lines) if lines else "(no evidence)"


def _call_ollama_chat(prompt: str) -> str:
    """Send ``prompt`` to the Ollama ``/api/chat`` endpoint.

    The model used is taken from the ``OLLAMA_CHAT_MODEL`` environment variable
    (default ``llama3:8b``). The function returns the ``content`` field of the
    first message in the response.
    """
    url = os.getenv("OLLAMA_HOST", "http://localhost:11434") + "/api/chat"
    payload = {
        "model": os.getenv("OLLAMA_CHAT_MODEL", "llama3:8b"),
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    resp = requests.post(url, json=payload, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama chat request failed: {resp.status_code} {resp.text}")
    data = resp.json()
    # Ollama returns a list of messages; take the first.
    if not data.get("message") or "content" not in data["message"]:
        raise RuntimeError("Unexpected Ollama chat response format")
    return data["message"]["content"].strip()


def answer_query(query: str, top_k: int = 5) -> str:
    """Answer ``query`` using retrieved evidence.

    1. Retrieve up to ``top_k`` relevant entities.
    2. Build a prompt that includes the evidence block.
    3. Call Ollama chat and return the model’s answer.
    """
    evidence = search(query, top_k=top_k)
    evidence_block = _format_evidence(evidence)
    prompt = (
        "You are ShelfSense, an evidence‑grounded product discovery assistant. "
        "Answer the user's question using ONLY the evidence provided below. "
        "If the evidence does not contain enough information, reply that you do not have sufficient data.\n\n"
        f"Evidence:\n{evidence_block}\n\nUser question: {query}\n\nAnswer:"
    )
    return _call_ollama_chat(prompt)
