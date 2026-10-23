"""
Database connection manager for Self Search.

Provides a thread-safe SQLite connection with WAL mode enabled
for better concurrent read performance.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Iterable, Optional

import structlog

logger = structlog.get_logger("shelfsense.db")


class SerializedCursor(sqlite3.Cursor):
    """Cursor that runs each statement *and fetches all its rows* under the connection lock.

    Rows are buffered, so ``fetchone``/``fetchall``/iteration never touch the
    connection again: another thread's statement cannot interleave with an
    active query and corrupt or empty its results.
    """

    def __init__(self, connection: "SerializedConnection") -> None:
        super().__init__(connection)
        self._buffer: list[Any] = []
        self._pos = 0

    def execute(self, sql: str, parameters: Any = (), /) -> "SerializedCursor":
        with self.connection.lock:  # type: ignore[attr-defined]
            super().execute(sql, parameters)
            self._buffer, self._pos = super().fetchall(), 0
        return self

    def executemany(self, sql: str, seq_of_parameters: Iterable[Any], /) -> "SerializedCursor":
        with self.connection.lock:  # type: ignore[attr-defined]
            super().executemany(sql, seq_of_parameters)
            self._buffer, self._pos = [], 0
        return self

    def executescript(self, sql_script: str, /) -> "SerializedCursor":
        with self.connection.lock:  # type: ignore[attr-defined]
            super().executescript(sql_script)
            self._buffer, self._pos = [], 0
        return self

    def fetchone(self) -> Any:
        if self._pos < len(self._buffer):
            row = self._buffer[self._pos]
            self._pos += 1
            return row
        return None

    def fetchmany(self, size: Optional[int] = None) -> list[Any]:
        n = self.arraysize if size is None else size
        rows = self._buffer[self._pos:self._pos + n]
        self._pos += len(rows)
        return rows

    def fetchall(self) -> list[Any]:
        rows = self._buffer[self._pos:]
        self._pos = len(self._buffer)
        return rows

    def __iter__(self) -> "SerializedCursor":
        return self

    def __next__(self) -> Any:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


class SerializedConnection(sqlite3.Connection):
    """A connection safe to share between threads: one re-entrant lock serialises it.

    Every statement executes and fully fetches under ``self.lock``
    (``SerializedCursor``). Callers that need several statements to be atomic
    (e.g. a SAVEPOINT transaction) hold ``self.lock`` for the whole block; the
    lock is re-entrant, so their own statements still run. There is only one
    lock, so lock-ordering deadlocks cannot occur.

    Python's C ``Connection.execute`` does not go through ``cursor()``, so the
    ``execute*`` shortcuts are overridden explicitly.

    Trade-off: database access is fully serialised. Each statement is short,
    but concurrent requests queue for the database (not for LLM generation,
    which runs without the lock).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lock = threading.RLock()

    def cursor(self, factory: Any = None) -> sqlite3.Cursor:  # type: ignore[override]
        return super().cursor(factory or SerializedCursor)  # type: ignore[call-overload]

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:  # type: ignore[override]
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, parameters: Iterable[Any], /) -> sqlite3.Cursor:  # type: ignore[override]
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:  # type: ignore[override]
        return self.cursor().executescript(sql_script)

    def commit(self) -> None:
        with self.lock:
            super().commit()

    def rollback(self) -> None:
        with self.lock:
            super().rollback()


def get_connection(db_path: str | Path, wal_mode: bool = True) -> sqlite3.Connection:
    """Create a new SQLite connection with recommended settings.

    Args:
        db_path: Path to the SQLite database file.
        wal_mode: Enable WAL journal mode for better concurrency.

    Returns:
        Configured sqlite3.Connection.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    if wal_mode:
        conn.execute("PRAGMA journal_mode = WAL")

    logger.debug("database_connected", path=str(db_path))
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Cursor, None, None]:
    """Context manager for a database transaction with automatic commit/rollback.

    Usage:
        with transaction(conn) as cur:
            cur.execute("INSERT INTO ...")

    Args:
        conn: An active SQLite connection.

    Yields:
        A cursor within the transaction.
    """
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@contextmanager
def managed_connection(
    db_path: str | Path, wal_mode: bool = True
) -> Generator[sqlite3.Connection, None, None]:
    """Context manager that opens, yields, and closes a database connection.

    Args:
        db_path: Path to the SQLite database file.
        wal_mode: Enable WAL journal mode.

    Yields:
        A configured sqlite3.Connection that is closed on exit.
    """
    conn = get_connection(db_path, wal_mode=wal_mode)
    try:
        yield conn
    finally:
        conn.close()
        logger.debug("database_closed", path=str(db_path))
