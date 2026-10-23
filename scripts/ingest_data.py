#!/usr/bin/env python
"""
CLI script to ingest data from HuggingFace into the Self Search database.

Usage:
    python scripts/ingest_data.py [--categories Electronics Sports_and_Outdoors]
                                   [--max-products 10000]
                                   [--max-reviews 100000]
                                   [--db-path data/processed/shelfsense.db]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config.logging import setup_logging
from src.config.settings import get_settings
from src.data.ingestion import ingest_from_huggingface
from src.data.statistics import format_report, generate_statistics
from src.db.connection import get_connection
from src.db.migrations import run_migrations


def main() -> None:
    """Run the data ingestion pipeline."""
    parser = argparse.ArgumentParser(description="Ingest data into Self Search database")
    parser.add_argument(
        "--categories",
        nargs="+",
        default=None,
        help="Categories to ingest (default: Electronics Sports_and_Outdoors)",
    )
    parser.add_argument(
        "--max-products",
        type=int,
        default=10_000,
        help="Maximum products per category (default: 10000)",
    )
    parser.add_argument(
        "--max-reviews",
        type=int,
        default=100_000,
        help="Maximum reviews per category (default: 100000)",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Database file path (default: from settings)",
    )
    args = parser.parse_args()

    settings = get_settings()
    logger = setup_logging(level=settings.log_level, log_format=settings.log_format)

    db_path = args.db_path or str(settings.resolved_db_path)
    logger.info("starting_ingestion", db_path=db_path)

    conn = get_connection(db_path)
    try:
        # Ensure schema is created
        run_migrations(conn)

        # Run ingestion
        stats = ingest_from_huggingface(
            conn,
            categories=args.categories,
            max_products=args.max_products,
            max_reviews=args.max_reviews,
        )

        # Print ingestion stats
        print("\n=== Ingestion Complete ===")
        print(json.dumps(stats, indent=2))

        # Generate and print data statistics report
        db_stats = generate_statistics(conn)
        report = format_report(db_stats)
        print("\n" + report)

        # Save report
        report_path = Path(db_path).parent / "data_statistics_report.md"
        report_path.write_text(report, encoding="utf-8")
        print(f"\nReport saved to: {report_path}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
