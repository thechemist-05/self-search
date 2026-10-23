"""Experimental schema v3 (price_estimates): explicit, repeatable, never on production."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.config.settings import get_settings
from src.db.connection import get_connection
from src.db.experimental_schema import (
    EXPERIMENTAL_SCHEMA_VERSION,
    ExperimentalSchemaError,
    apply_experimental_v3,
    has_price_estimates,
)
from src.db.migrations import _get_current_version, run_migrations
from src.db.schema import MIGRATIONS, SCHEMA_VERSION


def v2_db(path: Path) -> sqlite3.Connection:
    """A database at the current schema version (v4 since profile removal)."""
    conn = get_connection(path)
    run_migrations(conn)
    return conn


def test_applies_to_a_current_database(tmp_path: Path) -> None:
    conn = v2_db(tmp_path / "exp.db")
    assert apply_experimental_v3(conn) is True
    assert has_price_estimates(conn)
    assert EXPERIMENTAL_SCHEMA_VERSION == 3
    assert conn.execute("SELECT COUNT(*) FROM schema_version WHERE version = 3").fetchone()[0] == 1
    assert _get_current_version(conn) == SCHEMA_VERSION  # v3 is a marker, not the current version
    conn.close()


def test_repeat_is_a_no_op(tmp_path: Path) -> None:
    conn = v2_db(tmp_path / "exp.db")
    apply_experimental_v3(conn)
    assert apply_experimental_v3(conn) is False
    assert conn.execute("SELECT COUNT(*) FROM schema_version WHERE version = 3").fetchone()[0] == 1
    conn.close()


def test_not_part_of_the_regular_migrations() -> None:
    assert SCHEMA_VERSION == 4 and 3 not in MIGRATIONS


def test_regular_runner_leaves_v3_alone(tmp_path: Path) -> None:
    conn = v2_db(tmp_path / "exp.db")
    apply_experimental_v3(conn)
    run_migrations(conn)  # already current: no-op
    assert has_price_estimates(conn) and _get_current_version(conn) == SCHEMA_VERSION
    conn.close()


def test_fresh_database_gets_no_price_estimates(tmp_path: Path) -> None:
    conn = v2_db(tmp_path / "plain.db")
    assert not has_price_estimates(conn)
    conn.close()


def test_rejects_wrong_version(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "v0.db")
    with pytest.raises(ExperimentalSchemaError, match=f"expected schema v{SCHEMA_VERSION}"):
        apply_experimental_v3(conn)
    conn.close()


def test_rejects_half_applied_state(tmp_path: Path) -> None:
    conn = v2_db(tmp_path / "half.db")
    conn.execute("CREATE TABLE price_estimates (product_id INTEGER)")
    with pytest.raises(ExperimentalSchemaError, match="with price_estimates"):
        apply_experimental_v3(conn)
    conn.close()


def test_rejects_in_memory_database() -> None:
    conn = sqlite3.connect(":memory:")
    with pytest.raises(ExperimentalSchemaError, match="in-memory"):
        apply_experimental_v3(conn)


def test_refuses_production_and_backup_databases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    processed = tmp_path / "processed"
    processed.mkdir()
    monkeypatch.setenv("SHELFSENSE_DB_PATH", str(processed / "shelfsense.db"))
    monkeypatch.setenv("SHELFSENSE_DATA_PROCESSED_DIR", str(processed))
    get_settings.cache_clear()
    try:
        for name in ("shelfsense.db", "shelfsense.v1.backup.bak.db"):
            conn = v2_db(processed / name)
            with pytest.raises(ExperimentalSchemaError, match="production database or backup"):
                apply_experimental_v3(conn)
            assert not has_price_estimates(conn) and _get_current_version(conn) == SCHEMA_VERSION
            conn.close()
    finally:
        get_settings.cache_clear()


def test_real_production_database_is_protected() -> None:
    real = get_settings().resolved_db_path
    if not real.exists():
        pytest.skip("no production database on this machine")
    conn = sqlite3.connect(f"file:{real.as_posix()}?mode=ro", uri=True)
    with pytest.raises(ExperimentalSchemaError, match="production database"):
        apply_experimental_v3(conn)
    conn.close()


@pytest.mark.parametrize("sql", [
    "INSERT INTO price_estimates VALUES (1,'USD',100,50,200,'k','m','v','h','synthetic_estimate',datetime('now'))",
    "INSERT INTO price_estimates VALUES (1,'INR',300,50,200,'k','m','v','h','synthetic_estimate',datetime('now'))",
    "INSERT INTO price_estimates VALUES (1,'INR',100,50,200,'k','m','v','h','verified',datetime('now'))",
    "INSERT INTO price_estimates VALUES (1,'INR',100,200,50,'k','m','v','h','synthetic_estimate',datetime('now'))",
])
def test_table_constraints(experimental_conn, sql: str) -> None:
    experimental_conn.execute("INSERT INTO data_sources (id, name) VALUES (1, 's')")
    experimental_conn.execute("INSERT INTO products (id, source_id, external_id, title) VALUES (1, 1, 'x', 't')")
    with pytest.raises(sqlite3.IntegrityError):
        experimental_conn.execute(sql)
