"""
Database migration runner for Self Search.

Creates the version 1 baseline schema on a fresh database, then applies
incremental steps from ``MIGRATIONS`` in order. Each step runs in its own
transaction, so a failed step leaves the database at the previous version.
A step listed in ``REQUIRE_EMPTY`` is refused (nothing changed) while any of
its tables holds rows.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

import structlog

from src.db.schema import ALL_DDL, EXPECTED_TABLES, MIGRATIONS, REQUIRE_EMPTY, SCHEMA_VERSION

logger = structlog.get_logger("shelfsense.db.migrations")


class MigrationRefusedError(RuntimeError):
    """A destructive step would lose rows; the database was not changed."""


def run_migrations(conn: sqlite3.Connection, target_version: Optional[int] = None) -> None:
    """Apply all pending migrations to the database.

    Fresh databases get the version 1 baseline first. Every later version in
    ``MIGRATIONS`` above the current version (up to ``target_version``) is then
    applied in ascending order. Running this on an up-to-date database is a no-op.

    Args:
        conn: An active SQLite connection.
        target_version: Stop after this version (default: ``SCHEMA_VERSION``).

    Raises:
        MigrationRefusedError: a step's ``REQUIRE_EMPTY`` tables hold rows.
    """
    target = SCHEMA_VERSION if target_version is None else target_version
    current = _get_current_version(conn)

    if current >= target:
        logger.info("schema_up_to_date", current_version=current)
        return

    logger.info("applying_migrations", from_version=current, to_version=target)

    if current < 1:
        for ddl in ALL_DDL:
            conn.execute(ddl)
        _record_version(conn, 1)
        conn.commit()
        current = 1

    for version in sorted(v for v in MIGRATIONS if current < v <= target):
        _apply_step(conn, version, MIGRATIONS[version])

    logger.info("migrations_complete", version=_get_current_version(conn))


def _apply_step(conn: sqlite3.Connection, version: int, statements: list[str]) -> None:
    """Apply one migration step atomically; roll back entirely on any error."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN")
    try:
        non_empty = _non_empty_tables(conn, REQUIRE_EMPTY.get(version, ()))
        if non_empty:
            raise MigrationRefusedError(
                f"step {version} would delete rows in {non_empty}; export them first. "
                "Nothing was changed."
            )
        for stmt in statements:
            conn.execute(stmt)
        _record_version(conn, version)
        conn.commit()
    except Exception:
        conn.rollback()
        logger.error("migration_step_failed", version=version)
        raise
    logger.info("migration_step_applied", version=version)


def _non_empty_tables(conn: sqlite3.Connection, tables: tuple[str, ...]) -> dict[str, int]:
    """Row counts of the existing tables among ``tables`` that are not empty."""
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables if t in existing}
    return {t: n for t, n in counts.items() if n}


def _record_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO schema_version (version, applied_at) "
        "VALUES (?, datetime('now'))",
        (version,),
    )


def verify_schema(conn: sqlite3.Connection) -> list[str]:
    """Check that all expected tables exist.

    Args:
        conn: An active SQLite connection.

    Returns:
        List of missing table names. Empty if the schema is complete.
    """
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    )
    existing = {row[0] for row in cursor.fetchall()}

    missing = [t for t in EXPECTED_TABLES if t not in existing]

    if missing:
        logger.warning("schema_incomplete", missing_tables=missing)
    else:
        logger.info("schema_verified", table_count=len(EXPECTED_TABLES))

    return missing


def _get_current_version(conn: sqlite3.Connection) -> int:
    """Get the current schema version, or 0 if the table doesn't exist."""
    try:
        cursor = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        )
        row = cursor.fetchone()
        return row[0] if row and row[0] is not None else 0
    except sqlite3.OperationalError:
        return 0
