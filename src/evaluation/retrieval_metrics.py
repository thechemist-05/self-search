"""
Evaluation metrics for information retrieval (Recall@K, Precision@K, MRR).
"""

from __future__ import annotations

from typing import Sequence, Set


def recall_at_k(retrieved_ids: Sequence[int], relevant_ids: Set[int], k: int = 5) -> float:
    """Compute Recall@K: proportion of relevant items retrieved in top-K."""
    if not relevant_ids:
        return 0.0
    top_k = retrieved_ids[:k]
    hits = sum(1 for item in top_k if item in relevant_ids)
    return hits / len(relevant_ids)


def precision_at_k(retrieved_ids: Sequence[int], relevant_ids: Set[int], k: int = 5) -> float:
    """Compute Precision@K: proportion of top-K items that are relevant."""
    if k <= 0:
        return 0.0
    top_k = retrieved_ids[:k]
    if not top_k:
        return 0.0
    hits = sum(1 for item in top_k if item in relevant_ids)
    return hits / len(top_k)


def reciprocal_rank(retrieved_ids: Sequence[int], relevant_ids: Set[int]) -> float:
    """Compute Reciprocal Rank: 1 / (rank of first relevant item)."""
    if not relevant_ids:
        return 0.0
    for rank_0, item in enumerate(retrieved_ids):
        if item in relevant_ids:
            return 1.0 / (rank_0 + 1)
    return 0.0


def evaluate_retrieval_run(
    predictions: list[tuple[Sequence[int], Set[int]]],
    k_values: Sequence[int] = (1, 3, 5),
) -> dict[str, float]:
    """Compute aggregate retrieval metrics over a set of query predictions.

    Args:
        predictions: List of (retrieved_ids, relevant_ids_set) pairs.
        k_values: Cutoff ranks for Recall@K and Precision@K.

    Returns:
        Dictionary of mean metrics across all evaluated queries.
    """
    if not predictions:
        return {f"Recall@{k}": 0.0 for k in k_values} | {"MRR": 0.0}

    totals = {f"Recall@{k}": 0.0 for k in k_values}
    totals |= {f"Precision@{k}": 0.0 for k in k_values}
    totals["MRR"] = 0.0

    n = len(predictions)
    for retrieved, relevant in predictions:
        for k in k_values:
            totals[f"Recall@{k}"] += recall_at_k(retrieved, relevant, k=k)
            totals[f"Precision@{k}"] += precision_at_k(retrieved, relevant, k=k)
        totals["MRR"] += reciprocal_rank(retrieved, relevant)

    return {metric: round(total / n, 4) for metric, total in totals.items()}
