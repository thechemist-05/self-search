"""Experimental schema v3: synthetic INR price estimates, kept apart from source prices.

This step is deliberately NOT in ``src.db.schema.MIGRATIONS``: ``run_migrations``
would otherwise upgrade every database it touches, including production. It is
applied only by an explicit call to ``apply_experimental_v3`` on an experimental
or test database at the current schema version. It records version 3 as a marker
row in ``schema_version``; the current version stays ``SCHEMA_VERSION``. Older
experimental databases (v2 + v3, before step 4) keep ``price_estimates`` when
``run_migrations`` upgrades them, because step 4 drops only the profile tables.

``price_estimates`` never touches ``products.price`` / ``products.currency``,
which keep meaning "price supplied by the source". The evidence validator reads
only ``products.price``, so an estimate can never verify a price claim.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

import structlog

from src.db.migrations import _get_current_version, _record_version
from src.db.schema import SCHEMA_VERSION

logger = structlog.get_logger("shelfsense.db.experimental")

EXPERIMENTAL_SCHEMA_VERSION = 3

PRICE_ESTIMATES_DDL: list[str] = [
    """
    CREATE TABLE price_estimates (
        product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
        currency TEXT NOT NULL CHECK (currency = 'INR'),
        amount REAL NOT NULL CHECK (amount > 0),
        range_min REAL NOT NULL CHECK (range_min > 0),
        range_max REAL NOT NULL CHECK (range_max > range_min),
        rule_key TEXT NOT NULL,
        method TEXT NOT NULL,
        config_version TEXT NOT NULL,
        config_sha256 TEXT NOT NULL,
        label TEXT NOT NULL CHECK (label = 'synthetic_estimate'),
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        CHECK (amount BETWEEN range_min AND range_max)
    )
    """,
    "CREATE INDEX idx_price_estimates_amount ON price_estimates(amount)",
]


class ExperimentalSchemaError(RuntimeError):
    """The experimental migration was refused; the database was not changed."""


def _protected_paths() -> list[Path]:
    from src.config.settings import get_settings

    settings = get_settings()
    processed = settings.resolved_data_processed_dir
    return [settings.resolved_db_path.resolve(), *(p.resolve() for p in processed.glob("*.db"))]


def _database_file(conn: sqlite3.Connection) -> Optional[Path]:
    row = conn.execute("PRAGMA database_list").fetchone()  # (seq, name, file) of "main"
    return Path(row[2]).resolve() if row and row[2] else None


def has_price_estimates(conn: sqlite3.Connection) -> bool:
    """True if the database has the experimental ``price_estimates`` table."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'price_estimates'"
    ).fetchone() is not None


def _has_v3_marker(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM schema_version WHERE version = ?", (EXPERIMENTAL_SCHEMA_VERSION,)
    ).fetchone() is not None


def apply_experimental_v3(conn: sqlite3.Connection) -> bool:
    """Add ``price_estimates`` to an experimental database at the current schema version.

    Returns:
        True if applied, False if it was already applied (repeat call is a no-op).

    Raises:
        ExperimentalSchemaError: production database or its backups, an in-memory or
            unknown file, a version other than ``SCHEMA_VERSION``, or a half-applied state.
    """
    db_file = _database_file(conn)
    if db_file is None:
        raise ExperimentalSchemaError("refusing an in-memory or unnamed database; use an experimental file")
    if db_file in _protected_paths():
        raise ExperimentalSchemaError(f"refusing to migrate a production database or backup: {db_file}")

    version = _get_current_version(conn)
    exists = has_price_estimates(conn)
    marker = version > 0 and _has_v3_marker(conn)
    if exists and marker:
        logger.info("experimental_v3_already_applied", db=str(db_file))
        return False
    if version != SCHEMA_VERSION or exists or marker:
        raise ExperimentalSchemaError(
            f"expected schema v{SCHEMA_VERSION} without price_estimates; found v{version}"
            f"{' with' if exists else ' without'} price_estimates"
        )

    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN")
    try:
        for stmt in PRICE_ESTIMATES_DDL:
            conn.execute(stmt)
        _record_version(conn, EXPERIMENTAL_SCHEMA_VERSION)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    logger.info("experimental_v3_applied", db=str(db_file))
    return True
