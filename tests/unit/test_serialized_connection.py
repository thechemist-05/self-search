"""
Regression tests for the shared-connection concurrency defect (found in Phase 9).

One sqlite3 connection shared by API worker threads returned wrong or empty
results when queries overlapped. ``SerializedConnection`` executes and fully
fetches every statement under one re-entrant lock; explicit transactions
hold that lock end to end.
"""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.db.connection import SerializedConnection, SerializedCursor, get_connection

ROWS = 200


@pytest.fixture
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "s.db"), check_same_thread=False, factory=SerializedConnection)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE p (id INTEGER PRIMARY KEY, t TEXT)")
    c.executemany("INSERT INTO p (t) VALUES (?)", [(f"t{i}",) for i in range(ROWS)])
    c.commit()
    yield c
    c.close()


class TestCursorCompatibility:
    def test_all_entry_points_use_the_serialized_cursor(self, conn) -> None:
        assert isinstance(conn.execute("SELECT 1"), SerializedCursor)
        assert isinstance(conn.cursor(), SerializedCursor)
        assert isinstance(conn.executemany("INSERT INTO p (t) VALUES (?)", []), SerializedCursor)

    def test_fetch_methods_iteration_and_metadata(self, conn) -> None:
        cur = conn.execute("SELECT id, t FROM p WHERE id <= 5 ORDER BY id")
        assert cur.description[0][0] == "id"
        assert tuple(cur.fetchone()) == (1, "t0")
        assert [r["id"] for r in cur.fetchmany(2)] == [2, 3]
        assert [r["id"] for r in cur.fetchall()] == [4, 5]
        assert cur.fetchone() is None and cur.fetchall() == []
        assert [r[0] for r in conn.execute("SELECT id FROM p WHERE id <= 3 ORDER BY id")] == [1, 2, 3]

    def test_write_metadata_and_transactions(self, conn) -> None:
        cur = conn.execute("INSERT INTO p (t) VALUES ('new')")
        assert cur.lastrowid == ROWS + 1 and cur.rowcount == 1
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM p").fetchone()[0] == ROWS
        c2 = conn.cursor()
        c2.execute("UPDATE p SET t = 'x' WHERE id <= 10")
        assert c2.rowcount == 10
        conn.commit()
        conn.executescript("CREATE TABLE q (a); INSERT INTO q VALUES (1);")
        assert conn.execute("SELECT a FROM q").fetchone()[0] == 1

    def test_results_are_buffered_before_other_statements_run(self, conn) -> None:
        first = conn.execute("SELECT id FROM p WHERE id <= 3 ORDER BY id")
        for _ in range(50):
            conn.execute("SELECT COUNT(*) FROM p").fetchone()
        assert [r[0] for r in first.fetchall()] == [1, 2, 3]

    def test_plain_get_connection_is_unchanged(self, tmp_path: Path) -> None:
        plain = get_connection(tmp_path / "plain.db")
        try:
            assert type(plain) is sqlite3.Connection
        finally:
            plain.close()


class TestOverlappingQueries:
    @pytest.mark.parametrize("trial", range(3))
    def test_concurrent_queries_return_correct_results(self, conn, trial: int) -> None:
        """The workload that returned 1,833/4,000 wrong results on a plain shared connection."""
        wrong: list[int] = []

        def work(i: int) -> None:
            one = conn.execute("SELECT 1").fetchone()
            count = conn.execute("SELECT COUNT(*) FROM p WHERE id > ?", (i % 50,)).fetchall()
            row = conn.execute("SELECT t FROM p WHERE id = ?", (i % ROWS + 1,)).fetchone()
            if one is None or one[0] != 1 or count[0][0] != ROWS - i % 50 or row[0] != f"t{i % ROWS}":
                wrong.append(i)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(work, range(3000)))
        assert wrong == []


class TestTransactionsAndDeadlocks:
    def test_open_transaction_excludes_other_threads(self, tmp_path: Path) -> None:
        from src.api.app import open_api_connection

        c = open_api_connection(tmp_path / "tx.db")
        try:
            c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
            c.execute("INSERT INTO notes (id, body) VALUES (1, 'start')")
            c.commit()

            in_tx, release, seen = threading.Event(), threading.Event(), []

            def writer() -> None:
                # An explicit transaction holds the connection lock end to end.
                with c.lock:
                    c.execute("SAVEPOINT tx")
                    c.execute("UPDATE notes SET body = 'mid-transaction' WHERE id = 1")
                    in_tx.set()
                    release.wait(5)
                    c.execute("UPDATE notes SET body = 'committed' WHERE id = 1")
                    c.execute("RELEASE tx")
                    c.commit()

            def reader() -> None:
                seen.append(c.execute("SELECT body FROM notes WHERE id = 1").fetchone()[0])

            tw = threading.Thread(target=writer)
            tw.start()
            assert in_tx.wait(5)
            tr = threading.Thread(target=reader)
            tr.start()
            tr.join(0.3)
            assert tr.is_alive() and seen == []  # blocked while the transaction is open
            release.set()
            tw.join(5)
            tr.join(5)
            assert seen == ["committed"]  # never observed the half-finished transaction
        finally:
            c.close()

    def test_reentrant_use_does_not_deadlock(self, conn) -> None:
        done = threading.Event()

        def nested() -> None:
            with conn.lock:
                with conn.lock:
                    conn.execute("INSERT INTO p (t) VALUES ('nested')")
                    conn.commit()
                    conn.execute("SELECT COUNT(*) FROM p").fetchone()
            done.set()

        t = threading.Thread(target=nested)
        t.start()
        t.join(5)
        assert done.is_set(), "re-entrant locking deadlocked"
