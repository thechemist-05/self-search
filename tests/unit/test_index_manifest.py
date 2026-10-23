"""Database/index manifest: fingerprint, consistency checks, atomic write, startup verification."""

from __future__ import annotations

import json
import pickle
import sqlite3
from pathlib import Path

import pytest

from src.embeddings.embeddings import deterministic_embedding
from src.vector_store import manifest as mf
from src.vector_store.vector_store import VectorStore


def build_indexes(conn: sqlite3.Connection, index_dir: Path, review_dim: int = 64) -> None:
    """Consistent products/reviews indexes for every catalogue row."""
    products = conn.execute("SELECT id, title FROM products ORDER BY id").fetchall()
    reviews = conn.execute("SELECT id, text FROM reviews ORDER BY id").fetchall()
    VectorStore(str(index_dir / "products.index")).add_vectors(
        [deterministic_embedding(t) for _, t in products], "product", [i for i, _ in products])
    VectorStore(str(index_dir / "reviews.index")).add_vectors(
        [deterministic_embedding(t, dim=review_dim) for _, t in reviews], "review", [i for i, _ in reviews])


def checksums(index_dir: Path) -> dict[str, str]:
    return {p.name: mf.file_sha256(p) for p in sorted(index_dir.iterdir()) if p.is_file()}


@pytest.fixture
def env(tmp_path: Path, catalogue_db: Path):
    conn = sqlite3.connect(catalogue_db)
    index_dir = tmp_path / "vs"
    build_indexes(conn, index_dir)
    yield conn, index_dir
    conn.close()


class TestFingerprint:
    def test_deterministic(self, env) -> None:
        conn, _ = env
        assert mf.catalogue_fingerprint(conn) == mf.catalogue_fingerprint(conn)
        assert mf.catalogue_fingerprint(conn)["products"]["rows"] == 20
        assert mf.catalogue_fingerprint(conn)["reviews"]["rows"] == 100

    @pytest.mark.parametrize("sql", [
        "UPDATE products SET title = title || ' v2' WHERE id = 1",
        "UPDATE products SET description = 'changed' WHERE id = 2",
        "UPDATE reviews SET text = text || '!' WHERE id = 3",
        "DELETE FROM reviews WHERE id = 4",
        "INSERT INTO reviews (product_id, source_id, external_id, text) VALUES (1, 1, 'new-review', 'new')",
    ])
    def test_changes_with_the_catalogue(self, env, sql: str) -> None:
        conn, _ = env
        before = mf.catalogue_fingerprint(conn)
        conn.execute(sql)
        assert mf.catalogue_fingerprint(conn) != before

    def test_ignores_non_catalogue_tables(self, env) -> None:
        conn, _ = env
        before = mf.catalogue_fingerprint(conn)
        conn.execute("INSERT INTO query_logs (query, parsed_json) VALUES ('bags', '{}')")
        assert mf.catalogue_fingerprint(conn) == before


class TestConsistency:
    def test_consistent_pair_passes(self, env) -> None:
        conn, index_dir = env
        report = mf.check_index_consistency(conn, index_dir)
        assert report.ok, report.problems
        assert report.indexes["products"]["vectors"] == 20
        assert report.indexes["reviews"]["vectors"] == 100

    def test_indexed_row_missing_from_database(self, env) -> None:
        conn, index_dir = env
        conn.execute("DELETE FROM reviews WHERE id = 5")
        assert any("missing from reviews" in p for p in mf.check_index_consistency(conn, index_dir).problems)

    def test_unindexed_row(self, env) -> None:
        conn, index_dir = env
        conn.execute("INSERT INTO reviews (product_id, source_id, external_id, text) VALUES (1, 1, 'x', 'y')")
        assert any("rows not indexed" in p for p in mf.check_index_consistency(conn, index_dir).problems)

    def test_duplicate_vector_ids(self, env) -> None:
        conn, index_dir = env
        VectorStore(str(index_dir / "products.index")).add_vectors([deterministic_embedding("dup")], "product", [1])
        problems = mf.check_index_consistency(conn, index_dir).problems
        assert any("duplicate vector ids" in p for p in problems)

    def test_id_map_out_of_step(self, env) -> None:
        conn, index_dir = env
        pkl = index_dir / "products.pkl"
        id_map = pickle.loads(pkl.read_bytes())
        id_map.pop(1)
        pkl.write_bytes(pickle.dumps(id_map))
        problems = mf.check_index_consistency(conn, index_dir).problems
        assert any("id-map entries" in p for p in problems)

    def test_wrong_entity_type(self, env) -> None:
        conn, index_dir = env
        pkl = index_dir / "reviews.pkl"
        id_map = {k: ("product", v) for k, (_, v) in pickle.loads(pkl.read_bytes()).items()}
        pkl.write_bytes(pickle.dumps(id_map))
        assert any("are not 'review'" in p for p in mf.check_index_consistency(conn, index_dir).problems)

    def test_dimension_mismatch(self, tmp_path: Path, catalogue_db: Path) -> None:
        conn = sqlite3.connect(catalogue_db)
        build_indexes(conn, tmp_path / "vs32", review_dim=32)
        assert any("different dimensions" in p for p in mf.check_index_consistency(conn, tmp_path / "vs32").problems)
        conn.close()

    def test_missing_file(self, env) -> None:
        conn, index_dir = env
        (index_dir / "reviews.pkl").unlink()
        assert any("missing" in p for p in mf.check_index_consistency(conn, index_dir).problems)


class TestWriteAndVerify:
    def test_write_then_verify(self, env) -> None:
        conn, index_dir = env
        path = mf.write_manifest(conn, index_dir, embedder="test-embedder")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        assert set(manifest["indexes"]) == {"products", "reviews"}
        assert manifest["embedder"] == {"name": "test-embedder", "dimension": 64}
        assert manifest["excluded"] == {}
        assert mf.verify_manifest(conn, index_dir) == []
        assert not [p for p in index_dir.iterdir() if p.name.startswith(".")]  # no temp file left

    def test_refuses_inconsistent_pair_and_touches_nothing(self, env) -> None:
        conn, index_dir = env
        conn.execute("DELETE FROM reviews WHERE id = 7")
        before = checksums(index_dir)
        with pytest.raises(mf.IndexManifestError):
            mf.write_manifest(conn, index_dir, embedder="x")
        assert not (index_dir / mf.MANIFEST_FILE).exists()
        assert checksums(index_dir) == before

    def test_legacy_index_recorded_as_excluded(self, env) -> None:
        conn, index_dir = env
        VectorStore(str(index_dir / "faiss.index")).add_vectors([deterministic_embedding("a")] * 2, "product", [1, 1])
        legacy_sha = mf.file_sha256(index_dir / "faiss.index")
        manifest = json.loads(mf.write_manifest(conn, index_dir, embedder="x").read_text(encoding="utf-8"))
        assert manifest["excluded"]["faiss.index"]["sha256"] == legacy_sha
        assert "not verified" in manifest["excluded"]["faiss.index"]["reason"].lower()
        assert "faiss" not in json.dumps(manifest["indexes"])
        mf.require_valid_manifest(conn, index_dir)  # an inconsistent legacy index only warns
        assert mf.file_sha256(index_dir / "faiss.index") == legacy_sha

    def test_missing_manifest(self, env) -> None:
        conn, index_dir = env
        assert any("no manifest.json" in p for p in mf.verify_manifest(conn, index_dir))

    def test_corrupt_or_unsupported_manifest(self, env) -> None:
        conn, index_dir = env
        (index_dir / mf.MANIFEST_FILE).write_text("{not json", encoding="utf-8")
        assert any("unreadable" in p for p in mf.verify_manifest(conn, index_dir))
        (index_dir / mf.MANIFEST_FILE).write_text(json.dumps({"manifest_version": 99}), encoding="utf-8")
        assert any("unsupported" in p for p in mf.verify_manifest(conn, index_dir))

    def test_database_change_detected(self, env) -> None:
        conn, index_dir = env
        mf.write_manifest(conn, index_dir, embedder="x")
        conn.execute("UPDATE products SET title = 'Other product' WHERE id = 3")
        assert any("products differ" in p for p in mf.verify_manifest(conn, index_dir))

    @pytest.mark.parametrize("name", ["products.index", "reviews.pkl"])
    def test_index_file_change_detected(self, env, name: str) -> None:
        conn, index_dir = env
        mf.write_manifest(conn, index_dir, embedder="x")
        with open(index_dir / name, "ab") as fh:
            fh.write(b"\0")
        assert any(f"{name} changed" in p for p in mf.verify_manifest(conn, index_dir))

    def test_query_log_activity_keeps_manifest_valid(self, env) -> None:
        conn, index_dir = env
        mf.write_manifest(conn, index_dir, embedder="x")
        conn.execute("INSERT INTO query_logs (query) VALUES ('shoes')")
        assert mf.verify_manifest(conn, index_dir) == []

    def test_require_raises_with_problems(self, env) -> None:
        conn, index_dir = env
        with pytest.raises(mf.IndexManifestError) as exc:
            mf.require_valid_manifest(conn, index_dir)
        assert exc.value.problems

    def test_database_without_catalogue_tables(self, env, tmp_path: Path) -> None:
        conn, index_dir = env
        mf.write_manifest(conn, index_dir, embedder="x")
        empty = sqlite3.connect(tmp_path / "empty.db")
        assert any("cannot read the catalogue" in p for p in mf.verify_manifest(empty, index_dir))
        empty.close()
