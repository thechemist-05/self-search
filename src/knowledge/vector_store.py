import os
import sqlite3
import numpy as np
import faiss
from pathlib import Path

# Path to the persisted FAISS index file (relative to project root)
INDEX_PATH = Path(__file__).resolve().parents[2] / 'data' / 'faiss_index.faiss'

# SQLite mapping table for vector IDs to entity IDs
MAPPING_TABLE_SQL = '''
CREATE TABLE IF NOT EXISTS embedding_mapping (
    vector_id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,  -- 'product' or 'review'
    entity_id INTEGER NOT NULL,
    UNIQUE(entity_type, entity_id)
);
'''


def _ensure_mapping_table(conn: sqlite3.Connection) -> None:
    conn.execute(MAPPING_TABLE_SQL)
    conn.commit()


def get_faiss_index(dim: int = 768) -> faiss.IndexFlatL2:
    """Load an existing FAISS index or create a new one.

    The index is stored on disk at ``INDEX_PATH`` and loaded lazily. ``dim``
    defaults to 768 which matches the embedding size of the default Ollama
    model (llama3:8b). If you use a different model, adjust the dimension
    accordingly.
    """
    if INDEX_PATH.exists():
        return faiss.read_index(str(INDEX_PATH))
    return faiss.IndexFlatL2(dim)


def save_faiss_index(index: faiss.IndexFlatL2) -> None:
    """Persist the FAISS index to ``INDEX_PATH`` ensuring the directory exists."""
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(INDEX_PATH))


def add_vectors(
    vectors: np.ndarray,
    entity_type: str,
    entity_ids: list[int],
    conn: sqlite3.Connection,
) -> None:
    """Add a batch of vectors to the FAISS index and store the mapping.

    * ``vectors`` – shape ``(n, dim)``
    * ``entity_type`` – either ``"product"`` or ``"review"``
    * ``entity_ids`` – list of SQLite row IDs matching the vectors
    """
    if vectors.shape[0] != len(entity_ids):
        raise ValueError("Number of vectors must match number of entity IDs")

    index = get_faiss_index(dim=vectors.shape[1])
    cur = conn.cursor()
    _ensure_mapping_table(conn)
    cur.execute('SELECT MAX(vector_id) FROM embedding_mapping')
    max_id_row = cur.fetchone()
    start_id = (max_id_row[0] or -1) + 1

    # Add vectors to FAISS and persist
    index.add(vectors.astype(np.float32))
    save_faiss_index(index)

    # Insert mapping rows: vector_id = start_id + i
    rows = [(start_id + i, entity_type, eid) for i, eid in enumerate(entity_ids)]
    cur.executemany(
        'INSERT INTO embedding_mapping (vector_id, entity_type, entity_id) VALUES (?, ?, ?)',
        rows,
    )
    conn.commit()


def search_vectors(query_vec: np.ndarray, top_k: int = 10) -> list[int]:
    """Search the FAISS index and return the matching ``vector_id`` values.

    The caller can join the returned IDs with ``embedding_mapping`` to obtain the
    original ``entity_type`` and ``entity_id``.
    """
    index = get_faiss_index(dim=query_vec.shape[0])
    distances, indices = index.search(query_vec.astype(np.float32), top_k)
    # ``indices`` is a 2‑D ndarray; flatten and filter out -1 placeholders
    return [int(i) for i in indices[0] if i != -1]
