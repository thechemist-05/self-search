# src/vector_store/vector_store.py
"""FAISS vector store wrapper for Self Search.

The store persists a FAISS index on disk (default location
``data/vector_store/faiss.index``) and keeps a mapping from vector IDs to
SQLite row IDs (product or review).  It provides simple ``add_vectors`` and
``search`` methods.

The implementation is deliberately lightweight – it does not manage
metadata beyond the integer mapping, making it easy to mock in unit tests.
"""

import os
import pathlib
import pickle
from typing import List, Tuple

import faiss
import numpy as np

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_DEFAULT_INDEX_PATH = "data/vector_store/faiss.index"

# File names inside the configured vector-store directory.
PRODUCT_INDEX_FILE = "products.index"
REVIEW_INDEX_FILE = "reviews.index"
LEGACY_INDEX_FILE = "faiss.index"


def vector_store_dir() -> pathlib.Path:
    """The configured index directory (``SHELFSENSE_VECTOR_STORE_DIR``, default ``data/vector_store``)."""
    from src.config.settings import get_settings

    return pathlib.Path(get_settings().vector_store_dir)


def index_path_for(file_name: str, legacy_env: str) -> pathlib.Path:
    """Resolve an index path: the per-file legacy variable wins, then the configured directory."""
    explicit = os.getenv(legacy_env)
    return pathlib.Path(explicit) if explicit else vector_store_dir() / file_name

# FAISS uses float32 vectors. We'll fix dimensionality at runtime when the
# first vectors are added.

class VectorStore:
    """Simple FAISS wrapper handling persistence and ID mapping.

    The ``id_map`` dict stores ``vector_id -> (entity_type, entity_id)`` where
    ``entity_type`` is either ``"product"`` or ``"review"``.  This mapping is
    pickled alongside the FAISS index.
    """

    def __init__(self, index_path: str | None = None) -> None:
        if index_path is None:
            index_path = index_path_for(LEGACY_INDEX_FILE, "VECTOR_STORE_PATH")
        self._index_path = pathlib.Path(index_path)
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index: faiss.Index | None = None
        self.id_map: dict[int, Tuple[str, int]] = {}
        self._next_id: int = 0
        self._load()

    # ---------------------------------------------------------------------
    # Persistence helpers
    # ---------------------------------------------------------------------
    def _load(self) -> None:
        """Load index and mapping from disk if they exist.
        """
        if self._index_path.is_file():
            # FAISS stores its own binary; we store the mapping in a side‑car file.
            self.index = faiss.read_index(str(self._index_path))
            map_path = self._index_path.with_suffix('.pkl')
            if map_path.is_file():
                with open(map_path, "rb") as f:
                    self.id_map = pickle.load(f)
                # compute next id safely
                self._next_id = max(self.id_map.keys()) + 1 if self.id_map else 0
        else:
            self.index = None
            self.id_map = {}
            self._next_id = 0

    def _save(self) -> None:
        """Persist the FAISS index and the ID mapping.
        """
        if self.index is None:
            return
        faiss.write_index(self.index, str(self._index_path))
        map_path = self._index_path.with_suffix('.pkl')
        with open(map_path, "wb") as f:
            pickle.dump(self.id_map, f)

    # ---------------------------------------------------------------------
    # Index manipulation
    # ---------------------------------------------------------------------
    def _ensure_index(self, dim: int) -> None:
        """Create a new index if one does not already exist.

        The index must support ``add_with_ids`` because we explicitly provide
        external IDs for each vector. ``IndexFlatL2`` alone does not implement
        ``add_with_ids``, so we wrap it in ``faiss.IndexIDMap2`` which forwards
        all operations while storing the external IDs.
        """
        if self.index is None:
            # Base flat L2 index
            flat = faiss.IndexFlatL2(dim)
            # Wrap with an ID‑mapping index that implements ``add_with_ids``.
            self.index = faiss.IndexIDMap2(flat)
        elif self.index.d != dim:
            raise ValueError(
                f"Existing index dimension {self.index.d} does not match new vectors dimension {dim}"
            )

    def add_vectors(self, vectors: List[List[float]], entity_type: str, entity_ids: List[int]) -> None:
        """Add a batch of vectors to the store.

        Parameters
        ----------
        vectors:
            List of embedding vectors (list of floats).
        entity_type:
            ``"product"`` or ``"review"`` – stored for later lookup.
        entity_ids:
            Corresponding SQLite row IDs.
        """
        if len(vectors) != len(entity_ids):
            raise ValueError("vectors and entity_ids must have the same length")

        np_vectors = np.array(vectors, dtype=np.float32)
        dim = np_vectors.shape[1]
        self._ensure_index(dim)

        # Use the provided external entity IDs as FAISS IDs.
        ids = np.array(entity_ids, dtype=np.int64)

        # Update next internal counter to avoid collisions with future autogenerated IDs.
        if ids.size > 0:
            self._next_id = max(self._next_id, int(ids.max()) + 1)

        # Store mapping keyed by external ID.
        for ent_id in entity_ids:
            self.id_map[int(ent_id)] = (entity_type, int(ent_id))

        # Add to FAISS using external IDs.
        assert self.index is not None
        self.index.add_with_ids(np_vectors, ids)
        self._save()

    def search(self, query_vector: List[float], k: int = 5) -> List[Tuple[str, int, float]]:
        """Return the top‑k nearest neighbours.

        Returns a list of ``(entity_type, entity_id, distance)`` tuples ordered
        by increasing distance.
        """
        if self.index is None:
            return []
        q = np.array([query_vector], dtype=np.float32)
        distances, ids = self.index.search(q, k)
        results: List[Tuple[str, int, float]] = []
        for dist, idx in zip(distances[0], ids[0]):
            if idx == -1:
                continue
            meta = self.id_map.get(int(idx))
            if meta:
                entity_type, entity_id = meta
                results.append((entity_type, entity_id, float(dist)))
        return results

# Convenience singletons for library users.
_DEFAULT_PRODUCT_INDEX_PATH = "data/vector_store/products.index"
_DEFAULT_REVIEW_INDEX_PATH = "data/vector_store/reviews.index"

# Created on first use (not at import) so it honours the configured directory.
_default_store: VectorStore | None = None

def get_default_store() -> VectorStore:
    """Return the module‑level singleton ``VectorStore`` (legacy combined index)."""
    global _default_store
    if _default_store is None:
        _default_store = VectorStore()
    return _default_store

def get_product_store() -> VectorStore:
    """Return the vector store dedicated to product vectors."""
    return VectorStore(str(index_path_for(PRODUCT_INDEX_FILE, "PRODUCT_INDEX_PATH")))

def get_review_store() -> VectorStore:
    """Return the vector store dedicated to review vectors."""
    return VectorStore(str(index_path_for(REVIEW_INDEX_FILE, "REVIEW_INDEX_PATH")))

# End of file
