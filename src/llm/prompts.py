"""
Structured prompt construction for Self Search LLM generation (Phase 5).

Injects user questions, constraints, candidate products, and token-budgeted
evidence packs into a clear, instruction-grounded system and user prompt.

The fixed instructions (``SYSTEM_PROMPT``) always come first and never vary;
everything that varies (query, constraints, products, evidence) follows in the user
message. A shorter wording of these instructions and product lines was measured on
2026-10-22 and rejected: it saved about 15% of prompt tokens but roughly doubled
cross-product citations (docs/evaluation-results.md), so the text below is unchanged.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from src.evidence.evidence_pack import EvidencePack
from src.pricing.budget import classify_price
from src.pricing.fx import format_inr
from src.retrieval.filters import MetadataFilter


SYSTEM_PROMPT = """You are ShelfSense, an expert evidence-grounded conversational product discovery assistant.
Your goal is to answer the user's shopping inquiry using ONLY the retrieved product records and review evidence provided.

CRITICAL RULES:
1. You are the language layer, NOT the database and NOT the search engine.
2. DO NOT invent, hallucinate, or extrapolate products, prices, ratings, specs, or reviews.
3. Every factual claim in 'why_it_matches' and 'watch_outs' MUST reference the exact evidence IDs (e.g. ["R1", "R2"]) from the provided Review Evidence, or refer directly to verified product attributes.
4. If the retrieved evidence is empty, irrelevant, or insufficient to satisfy the user's specific request, you MUST set "insufficient_evidence": true and describe what is missing in "missing_information".
5. Be brief: recommend at most 3 products, with at most 2 "why_it_matches" claims and at most 1 "watch_outs" claim each. Keep the summary and every claim under 25 words. Use [] (never null) when a list is empty. Identify products by "product_id" only; do not repeat titles.
6. Return ONLY a single valid JSON object strictly matching this schema with no extra commentary or markdown formatting:

{
  "summary": "Concise factual summary answering the user's request.",
  "recommendations": [
    {
      "product_id": 1,
      "why_it_matches": [
        {"claim": "Factual statement matching user need.", "evidence_ids": ["R1", "R2"]}
      ],
      "watch_outs": [
        {"claim": "Important caveat or recurring complaint.", "evidence_ids": ["R3"]}
      ]
    }
  ],
  "insufficient_evidence": false,
  "missing_information": null
}"""


def format_price(price: Optional[float], currency: Optional[str]) -> str:
    """The price the model may state, always in INR (2026-10-23).

    An INR source price as is; a source price in another currency only as its documented
    INR conversion, with that provenance; otherwise "unavailable". The original foreign
    amount and synthetic estimates are never given to the model.
    """
    view = classify_price(price, currency, None, estimates_enabled=False)
    if view.amount is None:
        return "unavailable"
    if view.status == "converted" and view.fx is not None:
        return f"{format_inr(view.amount)} (converted from the source price at {view.fx.describe()})"
    return format_inr(view.amount)


def build_chat_messages(
    query: str,
    products: list[dict[str, Any]],
    evidence_pack: Optional[EvidencePack] = None,
    filters: Optional[MetadataFilter] = None,
) -> list[dict[str, str]]:
    """Build structured chat messages for the LLM inference call.

    Args:
        query: Raw user query string.
        products: Retrieved candidate products.
        evidence_pack: Compact, token-budgeted review evidence pack.
        filters: Optional user constraints / active filters.

    Returns:
        List of message dicts ready for LLMProvider.generate().
    """
    user_sections: list[str] = [f"USER QUERY: {query.strip()}"]

    # Constraints section
    if filters and not filters.is_empty():
        constraint_parts = []
        # Budgets are INR, like every price shown to the model.
        if filters.min_price is not None:
            constraint_parts.append(f"Min Price: {format_inr(filters.min_price)}")
        if filters.max_price is not None:
            constraint_parts.append(f"Max Price: {format_inr(filters.max_price)}")
        if filters.product_types:
            constraint_parts.append(f"Product type: {', '.join(filters.product_types)}")
        if filters.excluded_brands:
            constraint_parts.append(f"Excluded brands: {', '.join(filters.excluded_brands)}")
        if filters.excluded_terms:
            constraint_parts.append(f"Excluded: products mentioning {', '.join(filters.excluded_terms)}")
        if filters.min_rating is not None:
            constraint_parts.append(f"Min Rating: {filters.min_rating:.1f}+ stars")
        if filters.category:
            constraint_parts.append(f"Category: {filters.category}")
        if filters.brand:
            constraint_parts.append(f"Brand: {filters.brand}")
        if constraint_parts:
            user_sections.append("ACTIVE CONSTRAINTS:\n- " + "\n- ".join(constraint_parts))

    # Candidate products section
    if products:
        prod_lines = ["RETRIEVED CANDIDATE PRODUCTS:"]
        for p in products:
            price_str = format_price(p.get("price"), p.get("currency"))
            rating_str = f"{p.get('avg_rating', 0.0):.1f}/5.0" if p.get("avg_rating") else "N/A"
            brand_str = p.get("brand_name") or "Unknown"
            cat_str = p.get("category_name") or "General"
            desc = (p.get("description") or "").strip()
            if len(desc) > 160:
                desc = desc[:160] + "..."

            prod_lines.append(
                f"- Product ID: {p.get('id')} | ASIN: {p.get('external_id')} | Title: {p.get('title')}\n"
                f"  Brand: {brand_str} | Category: {cat_str} | Price: {price_str} | Rating: {rating_str}\n"
                f"  Description: {desc}"
            )
        user_sections.append("\n".join(prod_lines))
    else:
        user_sections.append("RETRIEVED CANDIDATE PRODUCTS: None found.")

    # Review evidence section
    if evidence_pack and evidence_pack.items:
        user_sections.append(evidence_pack.format_for_prompt())
    else:
        user_sections.append("REVIEW EVIDENCE: None available.")

    user_sections.append(
        "REMINDER: Generate valid JSON strictly following the schema. Cite evidence IDs (e.g. ['R1']) for all claims. "
        "Prices are in Indian rupees (₹) exactly as listed above; if a price is unavailable, say it is unavailable. "
        "Never state a price in another currency, convert one, or guess one."
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(user_sections)},
    ]
