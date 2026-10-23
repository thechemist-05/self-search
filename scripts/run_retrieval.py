#!/usr/bin/env python
"""
CLI script to run Self Search retrieval in any mode and display ranked results.

Usage:
    python scripts/run_retrieval.py --query "waterproof backpack" --mode hybrid --limit 5
    python scripts/run_retrieval.py --query "fast charger" --mode keyword --max-price 30
    python scripts/run_retrieval.py --query "bluetooth headphones" --mode semantic --with-evidence
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config.settings import get_settings
from src.db.connection import get_connection
from src.retrieval.filters import MetadataFilter
from src.retrieval.hybrid import HybridRetriever


def main() -> None:
    parser = argparse.ArgumentParser(description="Self Search Multi-Mode Retrieval CLI")
    parser.add_argument("--query", "-q", type=str, required=True, help="Search query string")
    parser.add_argument(
        "--mode",
        "-m",
        choices=["semantic", "keyword", "hybrid", "reranked_hybrid"],
        default="hybrid",
        help="Retrieval mode (default: hybrid)",
    )
    parser.add_argument("--limit", "-k", type=int, default=5, help="Number of results to display (default: 5)")
    parser.add_argument("--min-price", type=float, default=None, help="Minimum price filter")
    parser.add_argument("--max-price", type=float, default=None, help="Maximum price filter")
    parser.add_argument("--category", type=str, default=None, help="Category name filter")
    parser.add_argument("--brand", type=str, default=None, help="Brand name filter")
    parser.add_argument("--min-rating", type=float, default=None, help="Minimum average rating filter")
    parser.add_argument("--with-evidence", action="store_true", help="Also display review evidence pack")
    parser.add_argument("--budget", type=int, default=1500, help="Token budget for evidence pack (default: 1500)")
    parser.add_argument("--db-path", type=str, default=None, help="Optional database path override")

    args = parser.parse_args()
    settings = get_settings()
    db_path = args.db_path or str(settings.resolved_db_path)

    filters = MetadataFilter(
        category=args.category,
        brand=args.brand,
        min_price=args.min_price,
        max_price=args.max_price,
        min_rating=args.min_rating,
    )

    print("=" * 70)
    print(" SHELFSENSE RETRIEVAL")
    print(f" Query:     '{args.query}'")
    print(f" Mode:      {args.mode.upper()}")
    if not filters.is_empty():
        print(f" Filters:   min_price={args.min_price}, max_price={args.max_price}, cat={args.category}, brand={args.brand}, rating>={args.min_rating}")
    print("=" * 70)

    conn = get_connection(db_path)
    try:
        retriever = HybridRetriever(conn=conn, default_mode=args.mode)
        if args.with_evidence:
            products, evidence = retriever.retrieve_with_evidence(
                query=args.query,
                limit=args.limit,
                filters=filters,
                mode=args.mode,
                token_budget=args.budget,
            )
        else:
            products = retriever.retrieve(
                query=args.query,
                limit=args.limit,
                filters=filters,
                mode=args.mode,
            )
            evidence = None

        if not products:
            print("\nNo matching products found.")
            return

        print(f"\nRetrieved {len(products)} candidate product(s):\n")
        for rank, p in enumerate(products, start=1):
            price_str = f"${p.get('price', 0.0):.2f}" if p.get('price') is not None else "N/A"
            rating_str = f"{p.get('avg_rating', 0.0):.1f}/5.0 ({p.get('rating_count', 0)} reviews)" if p.get('avg_rating') else "No ratings"
            score_str = f"{p.get('score', 0.0):.4f}"
            source = p.get("retrieval_source", args.mode)

            print(f"[{rank}] ID: {p['id']} | ASIN: {p.get('external_id')} | Score: {score_str} ({source})")
            print(f"    Title:    {p.get('title', 'Untitled')}")
            print(f"    Brand:    {p.get('brand_name') or 'Unknown'} | Category: {p.get('category_name') or 'General'}")
            print(f"    Price:    {price_str} | Rating: {rating_str}")
            if "source_ranks" in p:
                print(f"    Ranks:    {p['source_ranks']}")
            print("-" * 70)

        if evidence and evidence.items:
            print("\n" + evidence.format_for_prompt())

    finally:
        conn.close()


if __name__ == "__main__":
    main()
