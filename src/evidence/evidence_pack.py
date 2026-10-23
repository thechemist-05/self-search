"""
Review evidence retrieval and token-budgeted evidence pack assembly (Phase 4).

Extracts and packages evidence snippets from customer reviews for top product candidates,
assigning consistent citation IDs (e.g. 'R1', 'R2') while enforcing a strict token budget.
With a query, each product's reviews are ranked by how many query terms they mention
(then helpful votes and rating), and near-duplicate texts are skipped, so the small
budget goes to the most relevant, distinct evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
import sqlite3
from typing import Any, Optional

# Words that say nothing about which review is relevant.
_QUERY_STOPWORDS = frozenset({
    "a", "an", "and", "any", "are", "best", "buy", "can", "does", "find", "for", "from", "get", "good",
    "how", "i", "in", "is", "it", "its", "looking", "me", "my", "need", "of", "on", "one", "or",
    "please", "show", "some", "than", "that", "the", "this", "to", "under", "want", "what", "which",
    "with", "without", "you", "above", "below", "between", "stars", "star", "rated", "rating",
})


def _query_terms(query: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2 and t not in _QUERY_STOPWORDS}


def _dedupe_key(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower())[:12])


def estimate_tokens(text: str) -> int:
    """Roughly estimate token count using character length (~4 characters per token)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


@dataclass
class EvidenceItem:
    """Single review evidence snippet with assigned citation ID."""

    evidence_id: str
    review_id: int
    product_id: int
    product_external_id: str
    title: str
    text: str
    rating: Optional[float] = None
    helpful_votes: int = 0
    verified: bool = False
    estimated_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidencePack:
    """Compact, token-budgeted container of review evidence for candidate products."""

    query: str
    items: list[EvidenceItem] = field(default_factory=list)
    total_tokens: int = 0
    token_budget: int = 1500
    product_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "total_tokens": self.total_tokens,
            "token_budget": self.token_budget,
            "product_ids": self.product_ids,
            "items": [item.to_dict() for item in self.items],
        }

    def format_for_prompt(self) -> str:
        """Format the evidence pack into a readable, citation-ready text block for an LLM."""
        if not self.items:
            return "No review evidence available."

        lines = ["REVIEW EVIDENCE (cite these IDs):"]
        for item in self.items:
            rating_str = f" [Rating: {item.rating}/5.0]" if item.rating else ""
            helpful_str = f" [{item.helpful_votes} helpful]" if item.helpful_votes > 0 else ""
            lines.append(
                f"[{item.evidence_id}] (Product ID: {item.product_id}, ASIN: {item.product_external_id})"
                f"{rating_str}{helpful_str}:"
            )
            if item.title:
                lines.append(f"  Title: {item.title}")
            lines.append(f"  Snippet: {item.text}")
            lines.append("")
        return "\n".join(lines).strip()


def assemble_evidence_pack(
    conn: sqlite3.Connection,
    product_ids: list[int],
    query: str = "",
    max_reviews_per_product: int = 3,
    token_budget: int = 1500,
    max_words: int = 120,
) -> EvidencePack:
    """Retrieve review evidence for top products and assemble a token-budgeted pack.

    Args:
        conn: SQLite database connection.
        product_ids: Ordered list of candidate product IDs.
        query: Optional user search query (used to prioritize relevant reviews).
        max_reviews_per_product: Maximum reviews to pull per product.
        token_budget: Maximum allowed tokens across the entire evidence pack.
        max_words: Review texts longer than this are truncated (with "...").

    Returns:
        EvidencePack instance containing prioritized EvidenceItem snippets.
    """
    if not product_ids:
        return EvidencePack(query=query, token_budget=token_budget)

    # Fetch product external IDs (ASINs)
    placeholders = ",".join("?" for _ in product_ids)
    prod_cursor = conn.cursor()
    prod_rows = prod_cursor.execute(
        f"SELECT id, external_id FROM products WHERE id IN ({placeholders})",
        product_ids,
    ).fetchall()
    asin_map = {row[0]: row[1] for row in prod_rows}

    evidence_items: list[EvidenceItem] = []
    total_tokens = 0
    evidence_counter = 1
    terms = _query_terms(query)

    # Fetch reviews for each product
    for pid in product_ids:
        # Pull reviews for this product ordered by helpful votes and rating
        rev_rows = conn.execute(
            """
            SELECT id, rating, title, text, helpful_votes, verified_flag
            FROM reviews
            WHERE product_id = ?
            ORDER BY helpful_votes DESC, rating DESC, id ASC
            LIMIT ?
            """,
            (pid, max(max_reviews_per_product * 2, 20) if terms else max_reviews_per_product * 2),
        ).fetchall()
        if terms:  # most query terms first; the SQL order breaks ties
            rev_rows = sorted(rev_rows, key=lambda r: -len(terms & _query_terms(f"{r[2] or ''} {r[3] or ''}")))

        count_for_prod = 0
        seen: set[str] = set()
        for r_id, rating, title, text, helpful, verified in rev_rows:
            if not text or not text.strip():
                continue
            key = _dedupe_key(text)
            if key in seen:
                continue
            seen.add(key)

            cleaned_text = text.strip()
            # Truncate long review texts so the budget covers more distinct reviews
            words = cleaned_text.split()
            if len(words) > max_words:
                cleaned_text = " ".join(words[:max_words]) + "..."

            snippet_tokens = estimate_tokens(cleaned_text) + estimate_tokens(title or "") + 15
            if total_tokens + snippet_tokens > token_budget:
                # If budget reached, finish assembly
                break

            evidence_id = f"R{evidence_counter}"
            evidence_counter += 1

            item = EvidenceItem(
                evidence_id=evidence_id,
                review_id=r_id,
                product_id=pid,
                product_external_id=asin_map.get(pid, ""),
                title=title or "",
                text=cleaned_text,
                rating=float(rating) if rating is not None else None,
                helpful_votes=helpful or 0,
                verified=bool(verified),
                estimated_tokens=snippet_tokens,
            )
            evidence_items.append(item)
            total_tokens += snippet_tokens
            count_for_prod += 1

            if count_for_prod >= max_reviews_per_product:
                break

    return EvidencePack(
        query=query,
        items=evidence_items,
        total_tokens=total_tokens,
        token_budget=token_budget,
        product_ids=product_ids,
    )
