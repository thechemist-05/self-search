"""
Data source registry for Self Search.

Tracks the provenance of every dataset: name, URL, licence,
download date, and processing notes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import structlog

from src.db.connection import transaction

logger = structlog.get_logger("shelfsense.data.registry")


@dataclass
class DataSource:
    """Metadata for a single data source."""

    id: Optional[int] = None
    name: str = ""
    url: str = ""
    license: str = ""
    downloaded_at: str = ""
    notes: str = ""


def register_source(
    conn: sqlite3.Connection,
    name: str,
    url: str = "",
    license: str = "",
    notes: str = "",
) -> int:
    """Register a data source or return its existing ID.

    Args:
        conn: Active database connection.
        name: Unique name for the data source.
        url: URL where the dataset can be found.
        license: Licence terms for the dataset.
        notes: Additional notes about the dataset.

    Returns:
        The integer ID of the data source row.
    """
    # Check if it already exists
    row = conn.execute(
        "SELECT id FROM data_sources WHERE name = ?", (name,)
    ).fetchone()

    if row:
        logger.info("source_exists", name=name, id=row[0])
        return row[0]

    now = datetime.utcnow().isoformat()
    with transaction(conn) as cur:
        cur.execute(
            "INSERT INTO data_sources (name, url, license, downloaded_at, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, url, license, now, notes),
        )
        source_id = cur.lastrowid

    logger.info("source_registered", name=name, id=source_id)
    return source_id


def get_source(conn: sqlite3.Connection, source_id: int) -> Optional[DataSource]:
    """Retrieve a data source by ID.

    Args:
        conn: Active database connection.
        source_id: ID of the data source.

    Returns:
        DataSource if found, None otherwise.
    """
    row = conn.execute(
        "SELECT id, name, url, license, downloaded_at, notes "
        "FROM data_sources WHERE id = ?",
        (source_id,),
    ).fetchone()

    if not row:
        return None

    return DataSource(
        id=row[0],
        name=row[1],
        url=row[2],
        license=row[3],
        downloaded_at=row[4],
        notes=row[5],
    )


def get_source_by_name(conn: sqlite3.Connection, name: str) -> Optional[DataSource]:
    """Retrieve a data source by name.

    Args:
        conn: Active database connection.
        name: Name of the data source.

    Returns:
        DataSource if found, None otherwise.
    """
    row = conn.execute(
        "SELECT id, name, url, license, downloaded_at, notes "
        "FROM data_sources WHERE name = ?",
        (name,),
    ).fetchone()

    if not row:
        return None

    return DataSource(
        id=row[0],
        name=row[1],
        url=row[2],
        license=row[3],
        downloaded_at=row[4],
        notes=row[5],
    )


def list_sources(conn: sqlite3.Connection) -> list[DataSource]:
    """List all registered data sources.

    Args:
        conn: Active database connection.

    Returns:
        List of all DataSource records.
    """
    rows = conn.execute(
        "SELECT id, name, url, license, downloaded_at, notes FROM data_sources"
    ).fetchall()

    return [
        DataSource(id=r[0], name=r[1], url=r[2], license=r[3], downloaded_at=r[4], notes=r[5])
        for r in rows
    ]
