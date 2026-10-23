"""scripts/verify_index_manifest.py: --check never writes; --write only for a consistent pair."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts import verify_index_manifest as script
from src.vector_store import manifest as mf
from tests.unit.test_index_manifest import build_indexes, checksums


@pytest.fixture
def pair(tmp_path: Path, catalogue_db: Path):
    conn = sqlite3.connect(catalogue_db)
    build_indexes(conn, tmp_path / "vs")
    conn.close()
    return catalogue_db, tmp_path / "vs"


def run(*argv: str) -> int:
    return script.main(list(argv))


def test_check_reports_and_never_writes(pair, capsys) -> None:
    db, index_dir = pair
    before = checksums(index_dir)
    assert run("--check", "--db", str(db), "--index-dir", str(index_dir)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["consistent"] is True and out["problems"] == []
    assert checksums(index_dir) == before


def test_write_records_unrecorded_embedder_and_validates(pair) -> None:
    db, index_dir = pair
    db_sha = mf.file_sha256(db)
    assert run("--write", "--db", str(db), "--index-dir", str(index_dir)) == 0
    manifest = json.loads((index_dir / mf.MANIFEST_FILE).read_text(encoding="utf-8"))
    assert manifest["embedder"]["name"] == script.UNRECORDED_EMBEDDER
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    assert mf.verify_manifest(conn, index_dir) == []
    conn.close()
    assert mf.file_sha256(db) == db_sha  # database opened read-only


def test_write_refuses_inconsistent_pair(pair, capsys) -> None:
    db, index_dir = pair
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM reviews WHERE id = 1")
    conn.commit()
    conn.close()
    before = checksums(index_dir)
    assert run("--write", "--db", str(db), "--index-dir", str(index_dir)) == 1
    assert "no manifest written" in capsys.readouterr().err
    assert checksums(index_dir) == before
    assert not (index_dir / mf.MANIFEST_FILE).exists()


def test_missing_database_is_a_usage_error(tmp_path: Path) -> None:
    assert run("--check", "--db", str(tmp_path / "nope.db"), "--index-dir", str(tmp_path)) == 2
