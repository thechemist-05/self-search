"""Index paths: SHELFSENSE_VECTOR_STORE_DIR, legacy per-file variables, lazy default store."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.config.settings import PROJECT_ROOT, get_settings
from src.vector_store import vector_store as vs

LEGACY_VARS = ("VECTOR_STORE_PATH", "PRODUCT_INDEX_PATH", "REVIEW_INDEX_PATH", "SHELFSENSE_VECTOR_STORE_DIR")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    for var in LEGACY_VARS:
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults_are_unchanged() -> None:
    assert vs.vector_store_dir() == Path("data/vector_store")
    assert vs.index_path_for(vs.PRODUCT_INDEX_FILE, "PRODUCT_INDEX_PATH") == Path("data/vector_store/products.index")
    assert vs.index_path_for(vs.REVIEW_INDEX_FILE, "REVIEW_INDEX_PATH") == Path("data/vector_store/reviews.index")
    assert vs.index_path_for(vs.LEGACY_INDEX_FILE, "VECTOR_STORE_PATH") == Path(vs._DEFAULT_INDEX_PATH)


def test_directory_setting_moves_every_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELFSENSE_VECTOR_STORE_DIR", str(tmp_path / "exp"))
    get_settings.cache_clear()
    assert vs.get_product_store()._index_path == tmp_path / "exp" / "products.index"
    assert vs.get_review_store()._index_path == tmp_path / "exp" / "reviews.index"
    assert vs.VectorStore()._index_path == tmp_path / "exp" / "faiss.index"


def test_legacy_per_file_variables_still_win(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELFSENSE_VECTOR_STORE_DIR", str(tmp_path / "dir"))
    monkeypatch.setenv("PRODUCT_INDEX_PATH", str(tmp_path / "p" / "prod.index"))
    monkeypatch.setenv("VECTOR_STORE_PATH", str(tmp_path / "legacy.index"))
    get_settings.cache_clear()
    assert vs.get_product_store()._index_path == tmp_path / "p" / "prod.index"
    assert vs.get_review_store()._index_path == tmp_path / "dir" / "reviews.index"  # not overridden
    assert vs.VectorStore()._index_path == tmp_path / "legacy.index"


def test_explicit_path_beats_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELFSENSE_VECTOR_STORE_DIR", str(tmp_path / "dir"))
    monkeypatch.setenv("VECTOR_STORE_PATH", str(tmp_path / "legacy.index"))
    get_settings.cache_clear()
    assert vs.VectorStore(str(tmp_path / "x.index"))._index_path == tmp_path / "x.index"


def test_import_creates_and_loads_nothing(tmp_path: Path) -> None:
    target = tmp_path / "never-created"
    env = {**os.environ, "SHELFSENSE_VECTOR_STORE_DIR": str(target), "PYTHONPATH": str(PROJECT_ROOT)}
    for var in LEGACY_VARS[:3]:
        env.pop(var, None)
    out = subprocess.run(
        [sys.executable, "-c", "import src.vector_store.vector_store as v; print(v._default_store is None)"],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "True"
    assert not target.exists()


def test_default_store_is_a_lazy_singleton(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VECTOR_STORE_PATH", str(tmp_path / "faiss.index"))
    monkeypatch.setattr(vs, "_default_store", None)
    first = vs.get_default_store()
    assert first is vs.get_default_store()
    assert first._index_path == tmp_path / "faiss.index"
