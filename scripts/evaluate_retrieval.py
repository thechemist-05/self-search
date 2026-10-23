#!/usr/bin/env python
"""
Run evaluation comparing retrieval variants on eval/queries.jsonl.

Usage:
    python scripts/evaluate_retrieval.py [--queries eval/queries.jsonl] [--k 5]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config.settings import get_settings
from src.db.connection import get_connection
from src.evaluation.retrieval_metrics import evaluate_retrieval_run
from src.retrieval.hybrid import HybridRetriever


def load_queries(path: Path) -> list[dict]:
    queries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                queries.append(json.loads(line))
    return queries


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Self Search Retrieval Modes")
    parser.add_argument(
        "--queries",
        type=str,
        default="eval/queries.jsonl",
        help="Path to evaluation queries JSONL file",
    )
    parser.add_argument("--k", type=int, default=5, help="Top-K cutoff for evaluation")
    args = parser.parse_args()

    settings = get_settings()
    queries_path = Path(args.queries)
    if not queries_path.is_absolute():
        queries_path = PROJECT_ROOT / queries_path

    queries = load_queries(queries_path)
    print(f"Loaded {len(queries)} evaluation queries from {queries_path}")

    conn = get_connection(settings.resolved_db_path)
    retriever = HybridRetriever(conn=conn)

    modes = ["keyword", "semantic", "hybrid", "reranked_hybrid"]
    results_by_mode: dict[str, dict[str, float]] = {}

    try:
        for mode in modes:
            predictions = []
            for q in queries:
                query_text = q["query"]
                relevant_ids = set(q["relevant_ids"])

                candidates = retriever.retrieve(query=query_text, limit=args.k, mode=mode)
                retrieved_ids = [c["id"] for c in candidates]
                predictions.append((retrieved_ids, relevant_ids))

            metrics = evaluate_retrieval_run(predictions, k_values=[1, 3, args.k])
            results_by_mode[mode] = metrics

        print("\n" + "=" * 70)
        print(" RETRIEVAL EVALUATION RESULTS")
        print("=" * 70)
        headers = ["Mode", f"Recall@1", f"Recall@3", f"Recall@{args.k}", "MRR"]
        row_format = "{:<18} {:<12} {:<12} {:<12} {:<10}"
        print(row_format.format(*headers))
        print("-" * 70)

        for mode, m in results_by_mode.items():
            r1 = f"{m.get('Recall@1', 0.0):.4f}"
            r3 = f"{m.get('Recall@3', 0.0):.4f}"
            rk = f"{m.get(f'Recall@{args.k}', 0.0):.4f}"
            mrr = f"{m.get('MRR', 0.0):.4f}"
            print(row_format.format(mode.upper(), r1, r3, rk, mrr))
        print("=" * 70)

        # Save results to eval/results/retrieval_evaluation.json
        output_dir = PROJECT_ROOT / "eval" / "results"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / "retrieval_evaluation.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results_by_mode, f, indent=2)
        print(f"\nEvaluation summary saved to: {output_file}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
