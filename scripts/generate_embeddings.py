# scripts/generate_embeddings.py
"""Rebuild the product and review vector indexes from the Self Search database.

1. Opens the database named by ``SHELFSENSE_DB_PATH`` (read-only).
2. Embeds every product (title + description) and review (text) with one
   embedder chosen for the whole build: Ollama if reachable, otherwise the
   offline 64-d feature-hashing embedder. The choice is recorded.
3. Builds ``products.index`` / ``reviews.index`` in a fresh temporary directory,
   checks them against the database, and writes ``manifest.json``.
4. Only then replaces the active files in ``SHELFSENSE_VECTOR_STORE_DIR``
   (indexes first, manifest last). If any step fails the previous indexes and
   manifest are left as they were.

The legacy combined ``faiss.index`` (read only by ``/ask``) is never written or
deleted; the manifest records it as excluded.

```bash
python scripts/generate_embeddings.py              # into an empty directory
python scripts/generate_embeddings.py --overwrite  # replace existing indexes
```
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import structlog

# Project imports – the script lives at the repository root, so we need to add
# the ``src`` package to ``sys.path``.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.config.settings import get_settings  # noqa: E402
from src.embeddings import embeddings as emb  # noqa: E402
from src.vector_store.manifest import (  # noqa: E402
    MANIFEST_FILE,
    IndexManifestError,
    verify_manifest,
    write_manifest,
)
from src.vector_store.vector_store import (  # noqa: E402
    PRODUCT_INDEX_FILE,
    REVIEW_INDEX_FILE,
    VectorStore,
    vector_store_dir,
)

log = structlog.get_logger("shelfsense.embedding_generator")

BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "500"))
OFFLINE_EMBEDDER = "offline-feature-hash-v1 (64-d)"
# Files this script owns in the index directory (manifest is replaced last).
_BUILT_FILES = [PRODUCT_INDEX_FILE, Path(PRODUCT_INDEX_FILE).with_suffix(".pkl").name,
                REVIEW_INDEX_FILE, Path(REVIEW_INDEX_FILE).with_suffix(".pkl").name]
_LEGACY_PATH_VARS = ("VECTOR_STORE_PATH", "PRODUCT_INDEX_PATH", "REVIEW_INDEX_PATH")


class IndexBuildError(RuntimeError):
    """The rebuild was refused or failed; the active indexes were not changed."""


def _fetch_products(conn) -> List[Tuple[int, str]]:
    """Return a list of ``(product_id, text)`` for all products.

    The *text* is ``title`` + ``description`` (both may be empty).  We join them
    with a newline to give the embedding model context.
    """
    rows = conn.execute(
        "SELECT id, title, description FROM products ORDER BY id"
    ).fetchall()
    return [(row[0], f"{row[1] or ''}\n{row[2] or ''}") for row in rows]


def _fetch_reviews(conn) -> List[Tuple[int, str]]:
    """Return a list of ``(review_id, text)`` for all reviews.
    """
    rows = conn.execute("SELECT id, text FROM reviews ORDER BY id").fetchall()
    return [(row[0], row[1] or "") for row in rows]


def choose_embedder() -> Tuple[str, Callable[[str], List[float]]]:
    """Pick one embedder for the whole build, so every vector has the same model and dimension."""
    if emb.ollama_embeddings_selected():  # explicit SHELFSENSE_EMBEDDING_BACKEND=ollama only
        return f"ollama:{emb._EMBEDDING_MODEL}", emb.get_embedding
    return OFFLINE_EMBEDDER, lambda text: emb.deterministic_embedding(text, dim=64)


def _store_vectors(
    store: VectorStore,
    items: List[Tuple[int, str]],
    entity_type: str,
    embed: Callable[[str], List[float]],
    batch_size: int = BATCH_SIZE,
) -> None:
    """Add vectors to *store* in batches.

    Parameters
    ----------
    store:
        ``VectorStore`` instance.
    items:
        List of ``(entity_id, text)``.
    entity_type:
        Either ``"product"`` or ``"review"`` – stored for later lookup.
    embed:
        The embedder chosen for this build.
    batch_size:
        Number of items embedded per ``add_vectors`` call.
    """
    total = len(items)
    log.info("embedding_start", entity_type=entity_type, total=total)
    for i in range(0, total, batch_size):
        batch = items[i : i + batch_size]
        ids, texts = zip(*batch)
        store.add_vectors([embed(t) for t in texts], entity_type, list(ids))
        log.info("embedding_batch_done", entity_type=entity_type, batch_start=i, batch_end=i + len(batch) - 1)
    log.info("embedding_complete", entity_type=entity_type, total=total)


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise IndexBuildError(f"database not found: {db_path}")
    return sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)


def rebuild_indexes(
    db_path: Optional[Path] = None,
    index_dir: Optional[Path] = None,
    *,
    overwrite: bool = False,
) -> Path:
    """Build, validate and atomically install the product and review indexes.

    Returns:
        Path of the installed ``manifest.json``.

    Raises:
        IndexBuildError: refused (existing indexes without ``overwrite``, legacy path
            variables set) or failed; the active indexes and manifest are unchanged.
    """
    set_vars = [v for v in _LEGACY_PATH_VARS if os.getenv(v)]
    if set_vars:
        raise IndexBuildError(f"unset {', '.join(set_vars)}; the build writes one directory "
                              "(SHELFSENSE_VECTOR_STORE_DIR) so a single manifest can cover it")
    db_path = Path(db_path) if db_path else get_settings().resolved_db_path
    index_dir = Path(index_dir) if index_dir else vector_store_dir()
    existing = [f for f in _BUILT_FILES + [MANIFEST_FILE] if (index_dir / f).exists()]
    if existing and not overwrite:
        raise IndexBuildError(f"{index_dir} already has {', '.join(existing)}; pass --overwrite to replace them")

    index_dir.mkdir(parents=True, exist_ok=True)
    embedder_name, embed = choose_embedder()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    build_dir = Path(tempfile.mkdtemp(prefix=f".build-{stamp}-", dir=index_dir))
    backup_dir = index_dir / f".previous-{stamp}"
    conn = _open_read_only(db_path)
    installed = False
    try:
        # 1. Build into a fresh directory, so nothing is appended to old vectors.
        _store_vectors(VectorStore(str(build_dir / PRODUCT_INDEX_FILE)), _fetch_products(conn), "product", embed)
        _store_vectors(VectorStore(str(build_dir / REVIEW_INDEX_FILE)), _fetch_reviews(conn), "review", embed)
        # 2. Validate and write the manifest (raises before anything is installed).
        #    The legacy faiss.index is only read there, to record its checksum.
        write_manifest(conn, build_dir, embedder=embedder_name, db_path=str(db_path), legacy_dir=index_dir)

        # 3. Install: back up the active files, replace indexes, manifest last.
        backup_dir.mkdir()
        for name in _BUILT_FILES + [MANIFEST_FILE]:
            if (index_dir / name).exists():
                shutil.copy2(index_dir / name, backup_dir / name)
        try:
            for name in _BUILT_FILES + [MANIFEST_FILE]:
                os.replace(build_dir / name, index_dir / name)
            problems = verify_manifest(conn, index_dir)
            if problems:
                raise IndexManifestError(problems)
        except Exception:
            for name in _BUILT_FILES + [MANIFEST_FILE]:
                if (backup_dir / name).exists():
                    os.replace(backup_dir / name, index_dir / name)
                elif (index_dir / name).exists():
                    (index_dir / name).unlink()
            raise
        installed = True
        log.info("indexes_installed", index_dir=str(index_dir), embedder=embedder_name)
        return index_dir / MANIFEST_FILE
    except IndexBuildError:
        raise
    except Exception as exc:
        raise IndexBuildError(f"index rebuild failed; active indexes unchanged: {exc}") from exc
    finally:
        conn.close()
        shutil.rmtree(build_dir, ignore_errors=True)
        # Keep the backup if a failed install could not be fully restored from it.
        if backup_dir.exists() and (installed or not any(backup_dir.iterdir())):
            shutil.rmtree(backup_dir, ignore_errors=True)
        elif backup_dir.exists():
            log.error("index_restore_incomplete", backup=str(backup_dir))


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Rebuild Self Search product and review indexes")
    parser.add_argument("--overwrite", action="store_true", help="replace existing indexes in the directory")
    args = parser.parse_args(argv)
    manifest = rebuild_indexes(overwrite=args.overwrite)
    log.info("embeddings_generation_finished", manifest=str(manifest))


if __name__ == "__main__":
    main()
