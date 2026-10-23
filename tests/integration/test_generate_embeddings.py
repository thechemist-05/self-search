"""scripts/generate_embeddings.py: configured paths, clean rebuild, validated atomic install.

Every test uses a temporary database and index directory; the real data/ is only
checksummed, to prove it is never touched.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from scripts import generate_embeddings as gen
from src.config.settings import PROJECT_ROOT, get_settings
from src.embeddings import embeddings as emb
from src.vector_store import manifest as mf
from src.vector_store.vector_store import VectorStore
from tests.unit.test_index_manifest import checksums


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch):
    """Deterministic offline embedder, isolated settings, no legacy path variables."""
    monkeypatch.setattr(emb, "check_ollama_available", lambda: False)
    for var in ("VECTOR_STORE_PATH", "PRODUCT_INDEX_PATH", "REVIEW_INDEX_PATH"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def ntotal(path: Path) -> int:
    return VectorStore(str(path)).index.ntotal


def test_uses_configured_database_and_directory_only(catalogue_db: Path, tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    real = PROJECT_ROOT / "data" / "vector_store"
    real_before = checksums(real)
    monkeypatch.setenv("SHELFSENSE_DB_PATH", str(catalogue_db))
    monkeypatch.setenv("SHELFSENSE_DB", str(tmp_path / "ignored.db"))  # the old variable is not read
    monkeypatch.setenv("SHELFSENSE_VECTOR_STORE_DIR", str(tmp_path / "exp"))
    get_settings.cache_clear()

    manifest_path = gen.rebuild_indexes()

    assert manifest_path == tmp_path / "exp" / "manifest.json"
    assert ntotal(tmp_path / "exp" / "products.index") == 20
    assert ntotal(tmp_path / "exp" / "reviews.index") == 100
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["embedder"] == {"name": gen.OFFLINE_EMBEDDER, "dimension": 64}
    assert not (tmp_path / "ignored.db").exists()
    assert checksums(real) == real_before


def test_refuses_existing_indexes_without_overwrite(catalogue_db: Path, tmp_path: Path) -> None:
    gen.rebuild_indexes(catalogue_db, tmp_path / "vs")
    before = checksums(tmp_path / "vs")
    with pytest.raises(gen.IndexBuildError, match="--overwrite"):
        gen.rebuild_indexes(catalogue_db, tmp_path / "vs")
    assert checksums(tmp_path / "vs") == before


def test_overwrite_rebuilds_without_duplicates_and_is_reproducible(catalogue_db: Path, tmp_path: Path) -> None:
    index_dir = tmp_path / "vs"
    gen.rebuild_indexes(catalogue_db, index_dir)
    first = {k: v for k, v in checksums(index_dir).items() if k != "manifest.json"}
    gen.rebuild_indexes(catalogue_db, index_dir, overwrite=True)
    assert ntotal(index_dir / "products.index") == 20  # rebuilt, not appended
    assert ntotal(index_dir / "reviews.index") == 100
    assert {k: v for k, v in checksums(index_dir).items() if k != "manifest.json"} == first
    conn = sqlite3.connect(catalogue_db)
    assert mf.verify_manifest(conn, index_dir) == []
    conn.close()
    assert not [p for p in index_dir.iterdir() if p.name.startswith(".")]  # no build/backup leftovers


def test_failed_validation_keeps_previous_indexes(catalogue_db: Path, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    index_dir = tmp_path / "vs"
    gen.rebuild_indexes(catalogue_db, index_dir)
    before = checksums(index_dir)

    def failing(*_a, **_k):
        raise mf.IndexManifestError(["simulated inconsistency"])

    monkeypatch.setattr(gen, "write_manifest", failing)
    with pytest.raises(gen.IndexBuildError, match="simulated inconsistency"):
        gen.rebuild_indexes(catalogue_db, index_dir, overwrite=True)
    assert checksums(index_dir) == before
    assert not [p for p in index_dir.iterdir() if p.name.startswith(".")]


def test_failure_during_install_restores_previous_files(catalogue_db: Path, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    index_dir = tmp_path / "vs"
    gen.rebuild_indexes(catalogue_db, index_dir)
    before = checksums(index_dir)
    conn = sqlite3.connect(catalogue_db)
    conn.execute("UPDATE products SET title = 'Changed title' WHERE id = 1")  # new build differs
    conn.commit()
    conn.close()
    real_replace = os.replace

    def flaky(src, dst):
        if Path(src).parent.name.startswith(".build-") and Path(dst).name == "manifest.json":
            raise OSError("simulated crash before the manifest swap")
        return real_replace(src, dst)

    monkeypatch.setattr(gen.os, "replace", flaky)
    with pytest.raises(gen.IndexBuildError, match="simulated crash"):
        gen.rebuild_indexes(catalogue_db, index_dir, overwrite=True)
    assert checksums(index_dir) == before
    assert not [p for p in index_dir.iterdir() if p.name.startswith(".")]


def test_legacy_index_is_never_written(catalogue_db: Path, tmp_path: Path) -> None:
    index_dir = tmp_path / "vs"
    VectorStore(str(index_dir / "faiss.index")).add_vectors([emb.deterministic_embedding("x")] * 2, "product", [1, 1])
    legacy = {k: v for k, v in checksums(index_dir).items()}
    manifest = json.loads(gen.rebuild_indexes(catalogue_db, index_dir).read_text(encoding="utf-8"))
    after = checksums(index_dir)
    assert {k: after[k] for k in legacy} == legacy
    assert manifest["excluded"]["faiss.index"]["sha256"] == legacy["faiss.index"]


def test_refuses_when_legacy_path_variables_are_set(catalogue_db: Path, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRODUCT_INDEX_PATH", str(tmp_path / "elsewhere.index"))
    with pytest.raises(gen.IndexBuildError, match="PRODUCT_INDEX_PATH"):
        gen.rebuild_indexes(catalogue_db, tmp_path / "vs")
    assert not (tmp_path / "vs").exists()


def test_missing_database_is_refused(tmp_path: Path) -> None:
    with pytest.raises(gen.IndexBuildError, match="database not found"):
        gen.rebuild_indexes(tmp_path / "nope.db", tmp_path / "vs")


def test_database_is_opened_read_only(catalogue_db: Path, tmp_path: Path) -> None:
    before = mf.file_sha256(catalogue_db)
    gen.rebuild_indexes(catalogue_db, tmp_path / "vs")
    assert mf.file_sha256(catalogue_db) == before


def test_one_embedder_per_build(monkeypatch: pytest.MonkeyPatch) -> None:
    assert gen.choose_embedder()[0] == gen.OFFLINE_EMBEDDER
    monkeypatch.setattr(emb, "check_ollama_available", lambda: True)
    # A reachable Ollama alone does not switch the embedder (it may serve only a chat model).
    assert gen.choose_embedder()[0] == gen.OFFLINE_EMBEDDER
    monkeypatch.setattr(emb, "_EMBEDDING_BACKEND", "ollama")
    name, fn = gen.choose_embedder()
    assert name.startswith("ollama:") and fn is emb.get_embedding
