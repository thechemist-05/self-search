# tests/unit/test_vector_store.py
"""Tests for the FAISS vector store wrapper (src.vector_store.vector_store)."""

import os
import shutil
from pathlib import Path

import numpy as np
import pytest

from src.vector_store.vector_store import VectorStore, get_default_store

@pytest.fixture
def temp_index_dir(tmp_path, monkeypatch):
    # Redirect the store to a temporary directory
    index_path = tmp_path / "faiss.index"
    monkeypatch.setenv("VECTOR_STORE_PATH", str(index_path))
    # Ensure a clean environment
    if index_path.exists():
        index_path.unlink()
    if (index_path.with_suffix('.pkl')).exists():
        (index_path.with_suffix('.pkl')).unlink()
    yield index_path
    # Cleanup
    if index_path.exists():
        index_path.unlink()
    if (index_path.with_suffix('.pkl')).exists():
        (index_path.with_suffix('.pkl')).unlink()

def test_add_and_search_vectors(temp_index_dir):
    store = VectorStore()
    # Create 3 simple 2‑dim vectors
    vectors = [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0]]
    entity_type = "product"
    entity_ids = [10, 20, 30]
    store.add_vectors(vectors, entity_type, entity_ids)

    # Search for a point near [1.1, 1.1]
    results = store.search([1.1, 1.1], k=2)
    # Expect the closest IDs to be 20 and 30 (distances order may vary)
    returned_ids = [eid for _, eid, _ in results]
    assert set(returned_ids) == {20, 30}
    # Ensure the mapping stores the correct entity type
    for _, eid, _ in results:
        assert store.id_map[eid][0] == entity_type

def test_persistence_across_instances(temp_index_dir):
    # First instance – add vectors
    store1 = VectorStore()
    vectors = [[0.5, 0.5], [1.5, 1.5]]
    store1.add_vectors(vectors, "review", [101, 102])
    # Force a reload by creating a new instance
    store2 = VectorStore()
    # The index should contain the same vectors
    results = store2.search([0.5, 0.5], k=2)
    ids = [eid for _, eid, _ in results]
    assert set(ids) == {101, 102}
    # Mapping should be preserved
    for eid in ids:
        assert store2.id_map[eid][0] == "review"
