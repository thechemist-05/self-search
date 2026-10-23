#!/usr/bin/env python
"""
CLI script to back up and migrate a Self Search database to the current schema.

A consistent backup is always written next to the database before any
migration step runs. Nothing is changed if the database is already current.
``--dry-run`` rehearses the migration on a temporary copy and leaves the
database itself untouched. Experimental databases (``data/experiments/``) are
refused unless ``--allow-experimental`` is given.

Usage:
    python scripts/migrate_db.py [--db-path data/processed/shelfsense.db] [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config.settings import PROJECT_ROOT, get_settings
from src.db.connection import get_connection
from src.db.migrations import _get_current_version, run_migrations, verify_schema
from src.db.schema import PROFILE_TABLES, SCHEMA_VERSION

EXPERIMENTS_DIR = PROJECT_ROOT / "data" / "experiments"

# Row counts that a migration must never change.
PRESERVED_TABLES = ("data_sources", "categories", "brands", "products", "reviews", "review_analysis",
                    "product_aspect_summary", "query_logs", "products_fts", "reviews_fts", "price_estimates")


def backup_database(conn: sqlite3.Connection, db_path: Path, version: int) -> Path:
    """Write a consistent copy of the database using SQLite's online backup API.

    Args:
        conn: Open connection to the database being backed up.
        db_path: Path of the database file (the backup is written beside it).
        version: Current schema version, recorded in the backup file name.

    Returns:
        Path of the backup file.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.with_name(f"{db_path.stem}.v{version}.{stamp}.bak{db_path.suffix}")
    dest = sqlite3.connect(str(backup_path))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return backup_path


def table_counts(conn: sqlite3.Connection, tables: tuple[str, ...]) -> dict[str, int]:
    """Row counts of the existing tables among ``tables``."""
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables if t in existing}


def post_checks(conn: sqlite3.Connection, preserved_before: dict[str, int]) -> list[str]:
    """Problems found after migrating (empty list: all checks passed)."""
    problems = []
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        problems.append(f"integrity_check: {integrity}")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        problems.append("foreign_key_check reported violations")
    after = table_counts(conn, PRESERVED_TABLES)
    if after != preserved_before:
        problems.append(f"preserved table counts changed: {preserved_before} -> {after}")
    return problems


def _migrate_open(conn: sqlite3.Connection) -> dict[str, Any]:
    before = _get_current_version(conn)
    preserved = table_counts(conn, PRESERVED_TABLES)
    profile = table_counts(conn, PROFILE_TABLES)
    run_migrations(conn)
    return {
        "from_version": before,
        "to_version": _get_current_version(conn),
        "profile_rows_before": profile,
        "profile_tables_after": sorted(table_counts(conn, PROFILE_TABLES)),
        "preserved_counts": preserved,
        "missing_tables": verify_schema(conn),
        "problems": post_checks(conn, preserved),
    }


def migrate(db_path: Path, dry_run: bool = False, allow_experimental: bool = False) -> dict:
    """Back up (if needed) and migrate the database at ``db_path``.

    Args:
        db_path: Existing SQLite database file.
        dry_run: Migrate a temporary copy instead; the database is not changed.
        allow_experimental: Permit a database under ``data/experiments/``.

    Returns:
        Summary dict with versions, backup path (or None), profile-table row counts,
        missing tables and post-migration problems.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    resolved = db_path.resolve()
    if not allow_experimental and EXPERIMENTS_DIR.resolve() in resolved.parents:
        raise ValueError(f"refusing an experimental database without --allow-experimental: {resolved}")

    if dry_run:
        with tempfile.TemporaryDirectory(prefix="ss-migrate-dry-") as tmp:
            src = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
            copy = Path(tmp) / "rehearsal.db"
            dst = sqlite3.connect(str(copy))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            conn = get_connection(copy)
            try:
                return {"db_path": resolved, "dry_run": True, "backup_path": None, **_migrate_open(conn)}
            finally:
                conn.close()

    conn = get_connection(db_path)
    try:
        before = _get_current_version(conn)
        backup_path: Optional[Path] = None
        if before < SCHEMA_VERSION:
            backup_path = backup_database(conn, db_path, before)
        return {"db_path": resolved, "dry_run": False, "backup_path": backup_path, **_migrate_open(conn)}
    finally:
        conn.close()


def main() -> None:
    """Back up and migrate the configured database."""
    parser = argparse.ArgumentParser(description="Back up and migrate the Self Search database")
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Database file path (default: from settings)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Rehearse on a temporary copy; the database is not changed")
    parser.add_argument("--allow-experimental", action="store_true",
                        help="Permit a database under data/experiments/")
    args = parser.parse_args()

    db_path = Path(args.db_path) if args.db_path else get_settings().resolved_db_path
    result = migrate(db_path, dry_run=args.dry_run, allow_experimental=args.allow_experimental)

    print(f"Database:        {result['db_path']}{'  (DRY RUN on a temporary copy)' if result['dry_run'] else ''}")
    print(f"Profile rows:    {result['profile_rows_before']}")
    if result["from_version"] == result["to_version"]:
        print(f"Already at schema version {result['to_version']}; nothing to do.")
    else:
        if result["backup_path"] is not None:
            print(f"Backup written:  {result['backup_path']}")
        print(f"Migrated schema: v{result['from_version']} -> v{result['to_version']}")
        print(f"Profile tables left: {result['profile_tables_after'] or 'none'}")
    if result["missing_tables"] or result["problems"]:
        print(f"WARNING: missing tables: {result['missing_tables']}; problems: {result['problems']}")
        if result["backup_path"] is not None:
            print(f"Restore from:    {result['backup_path']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
