"""Unit tests for database schema creation and migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.db.connection import get_connection, managed_connection, transaction
from src.db.migrations import run_migrations, verify_schema, _get_current_version
from src.db.schema import ALL_DDL, EXPECTED_TABLES, MIGRATIONS, SCHEMA_VERSION


class TestSchemaCreation:
    """Tests for schema creation and migration."""

    def test_schema_creates_all_tables(self, tmp_path: Path) -> None:
        """Running migrations creates all expected tables."""
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        try:
            run_migrations(conn)
            missing = verify_schema(conn)
            assert missing == [], f"Missing tables: {missing}"
        finally:
            conn.close()

    def test_schema_idempotent(self, tmp_path: Path) -> None:
        """Running migrations twice doesn't error or duplicate tables."""
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        try:
            run_migrations(conn)
            run_migrations(conn)  # Second run should be a no-op
            missing = verify_schema(conn)
            assert missing == []
        finally:
            conn.close()

    def test_schema_version_recorded(self, tmp_path: Path) -> None:
        """Schema version is recorded after migration."""
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        try:
            run_migrations(conn)
            version = _get_current_version(conn)
            assert version == SCHEMA_VERSION
        finally:
            conn.close()

    def test_fts5_tables_created(self, tmp_path: Path) -> None:
        """FTS5 virtual tables for full-text search are created."""
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        try:
            run_migrations(conn)
            # Check that FTS tables exist
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
            assert "products_fts" in tables
            assert "reviews_fts" in tables
        finally:
            conn.close()

    def test_foreign_keys_enabled(self, tmp_path: Path) -> None:
        """Foreign keys are enforced on the connection."""
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        try:
            result = conn.execute("PRAGMA foreign_keys").fetchone()
            assert result[0] == 1
        finally:
            conn.close()

    def test_products_unique_constraint(self, tmp_path: Path) -> None:
        """Duplicate (source_id, external_id) in products raises an error."""
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        try:
            run_migrations(conn)
            conn.execute(
                "INSERT INTO data_sources (name) VALUES (?)", ("test",)
            )
            conn.execute(
                "INSERT INTO products (source_id, external_id, title) VALUES (1, 'X1', 'Test')"
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO products (source_id, external_id, title) VALUES (1, 'X1', 'Dup')"
                )
        finally:
            conn.close()


class TestConnection:
    """Tests for the connection manager."""

    def test_managed_connection(self, tmp_path: Path) -> None:
        """managed_connection opens and closes the connection properly."""
        db_path = tmp_path / "test.db"
        with managed_connection(db_path) as conn:
            assert conn is not None
            conn.execute("SELECT 1")

    def test_transaction_commit(self, tmp_path: Path) -> None:
        """Transaction context manager commits on success."""
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        try:
            run_migrations(conn)
            with transaction(conn) as cur:
                cur.execute(
                    "INSERT INTO data_sources (name) VALUES (?)", ("test",)
                )
            row = conn.execute("SELECT name FROM data_sources").fetchone()
            assert row[0] == "test"
        finally:
            conn.close()

    def test_transaction_rollback(self, tmp_path: Path) -> None:
        """Transaction context manager rolls back on error."""
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        try:
            run_migrations(conn)
            with pytest.raises(ValueError):
                with transaction(conn) as cur:
                    cur.execute(
                        "INSERT INTO data_sources (name) VALUES (?)", ("rollback_test",)
                    )
                    raise ValueError("force rollback")
            row = conn.execute(
                "SELECT name FROM data_sources WHERE name = ?", ("rollback_test",)
            ).fetchone()
            assert row is None
        finally:
            conn.close()


def _make_v1_database(db_path: Path, profile_rows: bool = True) -> None:
    """Create a version 1 database (pre-Phase 8) containing some data."""
    conn = get_connection(db_path)
    try:
        for ddl in ALL_DDL:
            conn.execute(ddl)
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.execute("INSERT INTO data_sources (name) VALUES ('fixture')")
        conn.execute(
            "INSERT INTO products (source_id, external_id, title, price) VALUES (1, 'P1', 'Bag', 9.5)"
        )
        if profile_rows:
            conn.execute("INSERT INTO users (display_name) VALUES ('legacy')")
            conn.execute("INSERT INTO user_preferences (user_id, key, value) VALUES (1, 'budget_max', '50')")
            conn.execute("INSERT INTO wishlist (user_id, product_id) VALUES (1, 1)")
        conn.commit()
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


class TestMigrationV2:
    """Phase 8 schema v2 (history; step 4 later drops these tables): safe on v1 databases."""

    def test_fresh_database_reaches_v2(self, tmp_path: Path) -> None:
        conn = get_connection(tmp_path / "fresh.db")
        try:
            run_migrations(conn, target_version=2)
            assert _get_current_version(conn) == 2
            assert "personalization_consent" in _columns(conn, "users")
            assert {"source", "updated_at"} <= set(_columns(conn, "user_preferences"))
            assert verify_schema(conn) == []
        finally:
            conn.close()

    def test_v1_database_migrates_and_keeps_data(self, tmp_path: Path) -> None:
        db = tmp_path / "v1.db"
        _make_v1_database(db)
        conn = get_connection(db)
        try:
            assert _get_current_version(conn) == 1
            run_migrations(conn, target_version=2)
            assert _get_current_version(conn) == 2
            assert conn.execute("SELECT title, price FROM products").fetchone()[:] == ("Bag", 9.5)
            assert conn.execute("SELECT key, value, source FROM user_preferences").fetchone()[:] == (
                "budget_max", "50", "user_explicit"
            )
            assert conn.execute("SELECT personalization_consent FROM users").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM wishlist").fetchone()[0] == 1
            run_migrations(conn, target_version=2)  # second run is a no-op
            assert _get_current_version(conn) == 2
        finally:
            conn.close()

    def test_failed_step_rolls_back_to_v1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.db import migrations as mig

        db = tmp_path / "v1.db"
        _make_v1_database(db)
        monkeypatch.setitem(mig.MIGRATIONS, 2, MIGRATIONS[2] + ["THIS IS NOT SQL"])
        conn = get_connection(db)
        try:
            with pytest.raises(sqlite3.OperationalError):
                run_migrations(conn)
            assert _get_current_version(conn) == 1
            assert "personalization_consent" not in _columns(conn, "users")
            assert conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'feedback'"
            ).fetchone()[0] == 0
        finally:
            conn.close()

    def test_database_rejects_non_explicit_preference_source(self, tmp_path: Path) -> None:
        conn = get_connection(tmp_path / "fresh.db")
        try:
            run_migrations(conn, target_version=2)
            conn.execute("INSERT INTO users (display_name) VALUES ('u')")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO user_preferences (user_id, key, value, source) "
                    "VALUES (1, 'preferred_brands', '[\"X\"]', 'inferred')"
                )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE users SET personalization_consent = 2")
        finally:
            conn.close()

    def test_migrate_script_backs_up_before_migrating(self, tmp_path: Path) -> None:
        from scripts.migrate_db import migrate

        db = tmp_path / "v1.db"
        _make_v1_database(db, profile_rows=False)
        result = migrate(db)

        assert (result["from_version"], result["to_version"]) == (1, SCHEMA_VERSION)
        assert result["problems"] == []
        assert result["missing_tables"] == []
        backup = result["backup_path"]
        assert backup is not None and backup.exists()
        bconn = sqlite3.connect(str(backup))
        try:
            assert bconn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 1
            assert bconn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
        finally:
            bconn.close()

        assert migrate(db)["backup_path"] is None  # already current: no new backup


PROFILE = ("users", "user_preferences", "wishlist", "feedback", "conversations", "messages")


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _v2_database(db_path: Path) -> sqlite3.Connection:
    """A schema-v2 database (as production is before step 4) with catalogue rows."""
    conn = get_connection(db_path)
    run_migrations(conn, target_version=2)
    conn.execute("INSERT INTO data_sources (name) VALUES ('fixture')")
    conn.execute("INSERT INTO products (source_id, external_id, title, price) VALUES (1, 'P1', 'Bag', 9.5)")
    conn.execute("INSERT INTO reviews (product_id, source_id, external_id, text) VALUES (1, 1, 'R1', 'Good bag')")
    conn.commit()
    return conn


class TestMigrationV4:
    """Step 4 drops exactly the six profile tables, only when they are empty."""

    def test_exact_six_profile_tables(self) -> None:
        from src.db.schema import PROFILE_TABLES, REQUIRE_EMPTY

        assert set(PROFILE_TABLES) == set(PROFILE) and len(PROFILE_TABLES) == 6
        assert REQUIRE_EMPTY[4] == PROFILE_TABLES
        assert not set(PROFILE) & set(EXPECTED_TABLES)
        assert MIGRATIONS[4] == [f"DROP TABLE IF EXISTS {t}" for t in PROFILE_TABLES]

    def test_fresh_database_has_no_profile_tables(self, tmp_path: Path) -> None:
        conn = get_connection(tmp_path / "fresh.db")
        try:
            run_migrations(conn)
            assert _get_current_version(conn) == SCHEMA_VERSION == 4
            assert not _tables(conn) & set(PROFILE)
            assert verify_schema(conn) == []
        finally:
            conn.close()

    def test_v2_database_with_empty_profile_tables_migrates(self, tmp_path: Path) -> None:
        conn = _v2_database(tmp_path / "v2.db")
        try:
            run_migrations(conn)
            assert _get_current_version(conn) == 4
            assert not _tables(conn) & set(PROFILE)
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
            assert not {"idx_wishlist_user", "idx_feedback_user", "idx_user_preferences_user",
                        "idx_messages_conversation"} & names
            assert {"idx_products_price", "idx_reviews_product"} <= names
            assert conn.execute("SELECT title, price FROM products").fetchone()[:] == ("Bag", 9.5)
            assert conn.execute("SELECT COUNT(*) FROM reviews_fts WHERE reviews_fts MATCH 'bag'").fetchone()[0] == 1
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            conn.close()

    @pytest.mark.parametrize("rows", [
        ["INSERT INTO users (display_name) VALUES ('u')"],
        ["INSERT INTO users (display_name) VALUES ('u')", "INSERT INTO wishlist (user_id, product_id) VALUES (1, 1)"],
        ["INSERT INTO conversations (user_id) VALUES (NULL)"],
    ])
    def test_rows_in_any_profile_table_refuse_the_step(self, tmp_path: Path, rows: list[str]) -> None:
        from src.db.migrations import MigrationRefusedError

        conn = _v2_database(tmp_path / "v2.db")
        try:
            for sql in rows:
                conn.execute(sql)
            conn.commit()
            with pytest.raises(MigrationRefusedError, match="Nothing was changed"):
                run_migrations(conn)
            assert _get_current_version(conn) == 2
            assert set(PROFILE) <= _tables(conn)
            kept = sum(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in PROFILE)
            assert kept == len(rows)
        finally:
            conn.close()

    def test_failed_step_rolls_back_to_v2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.db import migrations as mig

        conn = _v2_database(tmp_path / "v2.db")
        monkeypatch.setitem(mig.MIGRATIONS, 4, MIGRATIONS[4][:3] + ["THIS IS NOT SQL"])
        try:
            with pytest.raises(sqlite3.OperationalError):
                run_migrations(conn)
            assert _get_current_version(conn) == 2
            assert set(PROFILE) <= _tables(conn)  # the three drops before the error were undone
        finally:
            conn.close()

    def test_legacy_experimental_database_keeps_price_estimates(self, tmp_path: Path) -> None:
        from src.db.experimental_schema import PRICE_ESTIMATES_DDL, apply_experimental_v3, has_price_estimates

        conn = _v2_database(tmp_path / "legacy-exp.db")
        try:
            for ddl in PRICE_ESTIMATES_DDL:  # what apply_experimental_v3 did before step 4 existed
                conn.execute(ddl)
            conn.execute("INSERT INTO schema_version (version) VALUES (3)")
            conn.execute("INSERT INTO price_estimates VALUES (1,'INR',800,500,1000,'k','m','v','h',"
                         "'synthetic_estimate',datetime('now'))")
            conn.commit()
            run_migrations(conn)
            assert _get_current_version(conn) == 4
            assert has_price_estimates(conn)
            assert conn.execute("SELECT amount FROM price_estimates").fetchone()[0] == 800
            assert apply_experimental_v3(conn) is False  # already applied
            assert not _tables(conn) & set(PROFILE)
        finally:
            conn.close()

    def test_migrate_script_refuses_profile_rows_after_backup(self, tmp_path: Path) -> None:
        from scripts.migrate_db import migrate
        from src.db.migrations import MigrationRefusedError

        db = tmp_path / "v1.db"
        _make_v1_database(db)  # has users / preferences / wishlist rows
        with pytest.raises(MigrationRefusedError):
            migrate(db)
        assert len(list(tmp_path.glob("v1.v1.*.bak.db"))) == 1
        conn = get_connection(db)
        try:
            assert _get_current_version(conn) == 2  # step 2 applied, step 4 refused
            assert conn.execute("SELECT COUNT(*) FROM wishlist").fetchone()[0] == 1
        finally:
            conn.close()

    def test_dry_run_leaves_the_database_unchanged(self, tmp_path: Path) -> None:
        from scripts.migrate_db import migrate
        from src.vector_store.manifest import file_sha256

        db = tmp_path / "v2.db"
        _v2_database(db).close()
        before = file_sha256(db)
        result = migrate(db, dry_run=True)
        assert result["dry_run"] and result["backup_path"] is None
        assert (result["from_version"], result["to_version"]) == (2, 4)
        assert result["profile_rows_before"] == {t: 0 for t in PROFILE}
        assert result["profile_tables_after"] == [] and result["problems"] == []
        assert result["preserved_counts"]["products"] == 1
        assert file_sha256(db) == before
        assert not list(tmp_path.glob("*.bak.db"))

    def test_migrate_script_refuses_experimental_databases(self, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts import migrate_db

        exp = tmp_path / "experiments" / "run-1"
        exp.mkdir(parents=True)
        db = exp / "shelfsense.db"
        _v2_database(db).close()
        monkeypatch.setattr(migrate_db, "EXPERIMENTS_DIR", tmp_path / "experiments")
        with pytest.raises(ValueError, match="experimental"):
            migrate_db.migrate(db)
        assert migrate_db.migrate(db, dry_run=True, allow_experimental=True)["to_version"] == 4
