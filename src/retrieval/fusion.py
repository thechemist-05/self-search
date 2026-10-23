"""
Reciprocal Rank Fusion (RRF) for Self Search hybrid retrieval.

Combines multiple ranked candidate lists (e.g. semantic FAISS and keyword BM25)
using the standard Reciprocal Rank Fusion formula:
    RRF(d) = sum_{m in M} (weight_m / (k + rank_m(d)))
where k is a smoothing constant (default 60) and rank_m(d) is 1-based.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence


def reciprocal_rank_fusion(
    ranked_lists: Mapping[str, Sequence[dict[str, Any]]],
    k: int = 60,
    weights: Optional[Mapping[str, float]] = None,
    limit: Optional[int] = None,
    id_key: str = "id",
) -> list[dict[str, Any]]:
    """Merge multiple ranked result lists using Reciprocal Rank Fusion.

    Args:
        ranked_lists: Dictionary mapping source name (e.g. 'keyword', 'semantic')
                      to an ordered sequence of candidate dicts.
        k: Smoothing constant for RRF (default: 60).
        weights: Optional dictionary mapping source name to relative weight (default: 1.0).
        limit: Optional maximum number of fused candidates to return.
        id_key: Dictionary key used to uniquely identify documents (default: 'id').

    Returns:
        List of merged candidate dicts sorted by 'rrf_score' in descending order.
        Each candidate includes 'rrf_score' and 'source_ranks'.
    """
    if not ranked_lists:
        return []

    item_store: dict[Any, dict[str, Any]] = {}
    rrf_scores: dict[Any, float] = {}
    source_ranks: dict[Any, dict[str, int]] = {}

    for source_name, items in ranked_lists.items():
        weight = weights.get(source_name, 1.0) if weights else 1.0
        for rank_0, item in enumerate(items):
            item_id = item.get(id_key)
            if item_id is None:
                continue

            rank_1 = rank_0 + 1  # 1-based rank
            score_contribution = weight / (k + rank_1)

            rrf_scores[item_id] = rrf_scores.get(item_id, 0.0) + score_contribution

            if item_id not in item_store:
                item_store[item_id] = dict(item)
            else:
                # Merge any missing fields from later occurrences
                for key, val in item.items():
                    if key not in item_store[item_id] or item_store[item_id][key] is None:
                        item_store[item_id][key] = val

            if item_id not in source_ranks:
                source_ranks[item_id] = {}
            source_ranks[item_id][source_name] = rank_1

    # Sort items by rrf_score descending
    sorted_ids = sorted(rrf_scores.keys(), key=lambda i: rrf_scores[i], reverse=True)

    if limit is not None:
        sorted_ids = sorted_ids[:limit]

    results: list[dict[str, Any]] = []
    for item_id in sorted_ids:
        candidate = dict(item_store[item_id])
        candidate["rrf_score"] = rrf_scores[item_id]
        candidate["source_ranks"] = source_ranks[item_id]
        candidate["score"] = rrf_scores[item_id]
        candidate["retrieval_source"] = "hybrid"
        results.append(candidate)

    return results
