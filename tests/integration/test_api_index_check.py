"""API startup refuses indexes that do not match the database; /populate rebuilds cleanly.

Uses temporary copies only (database and index directory).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from scripts import generate_embeddings as gen
from src.api import app as api_module
from src.api.app import app, set_agent_controller
from src.config.settings import get_settings
from src.embeddings import embeddings as emb
from src.vector_store import manifest as mf
from src.vector_store.vector_store import VectorStore


@pytest.fixture
def served(catalogue_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A database copy plus a manifest-valid index directory, wired through settings."""
    monkeypatch.setattr(emb, "check_ollama_available", lambda: False)
    for var in ("VECTOR_STORE_PATH", "PRODUCT_INDEX_PATH", "REVIEW_INDEX_PATH"):
        monkeypatch.delenv(var, raising=False)
    index_dir = tmp_path / "vs"
    gen.rebuild_indexes(catalogue_db, index_dir)
    monkeypatch.setenv("SHELFSENSE_DB_PATH", str(catalogue_db))
    monkeypatch.setenv("SHELFSENSE_VECTOR_STORE_DIR", str(index_dir))
    monkeypatch.setattr(api_module, "_API_KEY", None)
    get_settings.cache_clear()
    set_agent_controller(None)
    yield catalogue_db, index_dir
    if api_module._agent_controller is not None and not isinstance(api_module._agent_controller, MagicMock):
        api_module._agent_controller.conn.close()
    set_agent_controller(None)
    get_settings.cache_clear()


def start() -> TestClient:
    return TestClient(app)


def test_matching_manifest_serves(served) -> None:
    with start() as client:
        body = client.get("/health").json()
    assert body["status"] == "ok" and body["database"] == "connected"


def test_missing_manifest_refuses_startup(served) -> None:
    _, index_dir = served
    (index_dir / mf.MANIFEST_FILE).unlink()
    with pytest.raises(mf.IndexManifestError, match="no manifest.json"):
        with start():
            pass
    assert api_module._agent_controller is None


def test_changed_catalogue_refuses_startup(served) -> None:
    db, _ = served
    conn = sqlite3.connect(db)
    conn.execute("UPDATE reviews SET text = 'edited' WHERE id = 1")
    conn.commit()
    conn.close()
    with pytest.raises(mf.IndexManifestError, match="reviews differ"):
        with start():
            pass


def test_changed_index_file_refuses_startup(served) -> None:
    _, index_dir = served
    VectorStore(str(index_dir / "products.index")).add_vectors([emb.deterministic_embedding("x")], "product", [999])
    with pytest.raises(mf.IndexManifestError, match="products.index changed"):
        with start():
            pass


def test_corrupt_manifest_refuses_startup(served) -> None:
    _, index_dir = served
    (index_dir / mf.MANIFEST_FILE).write_text("{", encoding="utf-8")
    with pytest.raises(mf.IndexManifestError, match="unreadable"):
        with start():
            pass


def test_query_log_activity_does_not_block_startup(served) -> None:
    db, _ = served
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO query_logs (query) VALUES ('shoes')")
    conn.commit()
    conn.close()
    with start() as client:
        assert client.get("/health").status_code == 200


def test_injected_controller_skips_the_check(served) -> None:
    _, index_dir = served
    (index_dir / mf.MANIFEST_FILE).unlink()
    set_agent_controller(MagicMock())
    with start():
        pass  # no manifest needed: tests inject their own controller


def test_populate_rebuilds_cleanly(served) -> None:
    db, index_dir = served
    with start() as client:
        for _ in range(2):
            r = client.post("/populate")
            assert r.status_code == 200, r.text
            assert r.json() == {"status": "embeddings generated successfully"}
    assert VectorStore(str(index_dir / "products.index")).index.ntotal == 20  # not doubled
    assert VectorStore(str(index_dir / "reviews.index")).index.ntotal == 100
    conn = sqlite3.connect(db)
    assert mf.verify_manifest(conn, index_dir) == []
    conn.close()


def test_populate_failure_keeps_previous_indexes(served, monkeypatch: pytest.MonkeyPatch) -> None:
    _, index_dir = served
    with start() as client:
        before = {p.name: mf.file_sha256(p) for p in index_dir.iterdir() if p.is_file()}
        monkeypatch.setattr(gen, "write_manifest", lambda *a, **k: (_ for _ in ()).throw(
            mf.IndexManifestError(["simulated"])))
        r = client.post("/populate")
        assert r.status_code == 500 and "simulated" in r.json()["detail"]
    assert {p.name: mf.file_sha256(p) for p in index_dir.iterdir() if p.is_file()} == before
    assert json.loads((index_dir / mf.MANIFEST_FILE).read_text(encoding="utf-8"))["indexes"]
