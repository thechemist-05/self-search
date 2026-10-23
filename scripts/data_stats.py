#!/usr/bin/env python
"""
CLI script to generate a data statistics report from the Self Search database.

Usage:
    python scripts/data_stats.py [--db-path data/processed/shelfsense.db]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config.logging import setup_logging
from src.config.settings import get_settings
from src.data.statistics import format_report, generate_statistics
from src.db.connection import get_connection


def main() -> None:
    """Generate and display data statistics."""
    parser = argparse.ArgumentParser(description="Generate Self Search data statistics")
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Database file path (default: from settings)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output file path for the report (default: print to stdout)",
    )
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(level=settings.log_level, log_format=settings.log_format)

    db_path = args.db_path or str(settings.resolved_db_path)

    conn = get_connection(db_path)
    try:
        stats = generate_statistics(conn)
        report = format_report(stats)

        if args.output:
            Path(args.output).write_text(report, encoding="utf-8")
            print(f"Report saved to: {args.output}")
        else:
            print(report)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
