# src/vector_store/manifest.py
"""Database/index manifest: ties a vector-store directory to the catalogue it was built from.

FAISS IDs are SQLite row IDs, so an index built from one database returns wrong
products when served against another. ``manifest.json`` records a fingerprint of
the catalogue (products and reviews only; other tables such as query logs are excluded so
activity never invalidates it) plus the sha256 of every covered index file.

* ``check_index_consistency`` inspects the index files against the database.
* ``write_manifest`` writes the manifest atomically, and only after that check passes.
* ``require_valid_manifest`` is the API startup check: it raises
  ``IndexManifestError`` when the manifest is missing or does not match.

The legacy combined ``faiss.index`` (read only by ``/ask``) is not covered: it is
recorded under ``excluded`` with its checksum and the reason, and only warned about.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import structlog

from src.vector_store.vector_store import LEGACY_INDEX_FILE, PRODUCT_INDEX_FILE, REVIEW_INDEX_FILE

logger = structlog.get_logger(__name__)

MANIFEST_FILE = "manifest.json"
MANIFEST_VERSION = 1

# name -> (index file, entity type stored in the id map, catalogue table)
COVERED_INDEXES: dict[str, tuple[str, str, str]] = {
    "products": (PRODUCT_INDEX_FILE, "product", "products"),
    "reviews": (REVIEW_INDEX_FILE, "review", "reviews"),
}

# Text that is embedded, so a change to it means the vectors are stale.
_FINGERPRINT_SQL = {
    "products": "SELECT id, source_id, external_id, title, description FROM products ORDER BY id",
    "reviews": "SELECT id, product_id, source_id, external_id, title, text FROM reviews ORDER BY id",
}


class IndexManifestError(RuntimeError):
    """The vector indexes cannot be trusted to match the database."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass
class ConsistencyReport:
    """Result of comparing index files with the database."""

    problems: list[str] = field(default_factory=list)
    indexes: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems


def file_sha256(path: Path) -> str:
    """sha256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def catalogue_fingerprint(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Row count and sha256 over the identity and embedded text of products and reviews."""
    out: dict[str, dict[str, Any]] = {}
    for table, sql in _FINGERPRINT_SQL.items():
        digest = hashlib.sha256()
        count = 0
        for row in conn.execute(sql).fetchall():
            digest.update(json.dumps([*row], ensure_ascii=False, default=str).encode("utf-8"))
            digest.update(b"\n")
            count += 1
        out[table] = {"rows": count, "sha256": digest.hexdigest()}
    return out


def _load_index(index_file: Path) -> tuple[Any, dict[int, tuple[str, int]]]:
    import faiss

    index = faiss.read_index(str(index_file))
    with open(index_file.with_suffix(".pkl"), "rb") as fh:
        id_map = pickle.load(fh)
    return index, id_map


def check_index_consistency(conn: sqlite3.Connection, index_dir: Path) -> ConsistencyReport:
    """Check every covered index against the database. Read-only."""
    import faiss

    report = ConsistencyReport()
    dims: set[int] = set()
    for name, (file_name, entity_type, table) in COVERED_INDEXES.items():
        index_file = Path(index_dir) / file_name
        map_file = index_file.with_suffix(".pkl")
        if not index_file.is_file() or not map_file.is_file():
            report.problems.append(f"{name}: missing {index_file.name} or {map_file.name}")
            continue
        try:
            index, id_map = _load_index(index_file)
        except Exception as exc:  # unreadable or corrupt files
            report.problems.append(f"{name}: cannot read index: {exc}")
            continue
        ids = [int(i) for i in faiss.vector_to_array(index.id_map)] if hasattr(index, "id_map") else []
        db_ids = {int(r[0]) for r in conn.execute(f"SELECT id FROM {table}").fetchall()}
        problems = []
        if index.ntotal != len(id_map):
            problems.append(f"{index.ntotal} vectors but {len(id_map)} id-map entries")
        if len(ids) != len(set(ids)):
            problems.append(f"{len(ids) - len(set(ids))} duplicate vector ids")
        if set(ids) != set(id_map):
            problems.append("vector ids and id-map keys differ")
        wrong = [k for k, (et, eid) in id_map.items() if et != entity_type or int(eid) != int(k)]
        if wrong:
            problems.append(f"{len(wrong)} id-map entries are not '{entity_type}' or do not map to themselves")
        if missing := set(id_map) - db_ids:
            problems.append(f"{len(missing)} indexed ids missing from {table}")
        if unindexed := db_ids - set(id_map):
            problems.append(f"{len(unindexed)} {table} rows not indexed")
        report.problems.extend(f"{name}: {p}" for p in problems)
        dims.add(int(index.d))
        report.indexes[name] = {
            "file": file_name, "sha256": file_sha256(index_file),
            "map_file": map_file.name, "map_sha256": file_sha256(map_file),
            "vectors": int(index.ntotal), "dimension": int(index.d),
        }
    if len(dims) > 1:
        report.problems.append(f"covered indexes have different dimensions: {sorted(dims)}")
    return report


def _excluded_legacy(index_dir: Path) -> dict[str, Any]:
    legacy = Path(index_dir) / LEGACY_INDEX_FILE
    if not legacy.is_file():
        return {}
    entry: dict[str, Any] = {
        "sha256": file_sha256(legacy),
        "reason": "Legacy combined index read only by /ask; products and reviews share integer ids "
                  "and collide in it. Not covered by this manifest and not verified.",
    }
    if legacy.with_suffix(".pkl").is_file():
        entry["map_sha256"] = file_sha256(legacy.with_suffix(".pkl"))
    return {LEGACY_INDEX_FILE: entry}


def write_manifest(
    conn: sqlite3.Connection,
    index_dir: Path,
    *,
    embedder: str,
    db_path: Optional[str] = None,
    legacy_dir: Optional[Path] = None,
) -> Path:
    """Write ``manifest.json`` atomically after a passing consistency check.

    ``legacy_dir`` is where to look for the excluded legacy ``faiss.index``
    (default: ``index_dir``); it is only read, to record its checksum.

    Raises:
        IndexManifestError: if any covered index is inconsistent; nothing is written.
    """
    index_dir = Path(index_dir)
    report = check_index_consistency(conn, index_dir)
    if not report.ok:
        raise IndexManifestError(report.problems)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "db_path": db_path,
        "catalogue": catalogue_fingerprint(conn),
        "embedder": {"name": embedder, "dimension": next(iter(report.indexes.values()))["dimension"]},
        "indexes": report.indexes,
        "excluded": _excluded_legacy(Path(legacy_dir) if legacy_dir else index_dir),
    }
    target = index_dir / MANIFEST_FILE
    tmp = index_dir / f".{MANIFEST_FILE}.tmp-{os.getpid()}"
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)
    return target


def verify_manifest(conn: sqlite3.Connection, index_dir: Path) -> list[str]:
    """Problems that make the indexes untrustworthy for this database (empty list = valid)."""
    target = Path(index_dir) / MANIFEST_FILE
    if not target.is_file():
        return [f"no {MANIFEST_FILE} in {index_dir}; build indexes with scripts/generate_embeddings.py "
                f"or verify existing ones with scripts/verify_index_manifest.py --write"]
    try:
        manifest = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"unreadable {MANIFEST_FILE}: {exc}"]
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        return [f"unsupported manifest_version {manifest.get('manifest_version')!r}"]
    problems = []
    try:
        current = catalogue_fingerprint(conn)
    except sqlite3.Error as exc:
        return [f"cannot read the catalogue from the database: {exc}"]
    for table, recorded in (manifest.get("catalogue") or {}).items():
        now = current.get(table)
        if now != recorded:
            problems.append(f"database {table} differ from the indexed catalogue "
                            f"({recorded.get('rows')} rows indexed, {now and now['rows']} rows now)")
    if set(manifest.get("catalogue") or {}) != set(current):
        problems.append("manifest does not cover the expected catalogue tables")
    for name in COVERED_INDEXES:
        entry = (manifest.get("indexes") or {}).get(name)
        if not entry:
            problems.append(f"manifest does not cover {name}")
            continue
        for key, file_key in (("sha256", "file"), ("map_sha256", "map_file")):
            path = Path(index_dir) / entry[file_key]
            if not path.is_file():
                problems.append(f"{entry[file_key]} is missing")
            elif file_sha256(path) != entry[key]:
                problems.append(f"{entry[file_key]} changed since the manifest was written")
    return problems


def require_valid_manifest(conn: sqlite3.Connection, index_dir: Path) -> None:
    """API startup check: raise if the indexes do not match the database; warn about the legacy index."""
    problems = verify_manifest(conn, index_dir)
    if problems:
        raise IndexManifestError(problems)
    manifest = json.loads((Path(index_dir) / MANIFEST_FILE).read_text(encoding="utf-8"))
    legacy = Path(index_dir) / LEGACY_INDEX_FILE
    if legacy.is_file():
        recorded = (manifest.get("excluded") or {}).get(LEGACY_INDEX_FILE, {})
        logger.warning(
            "legacy_index_excluded_unverified",
            file=str(legacy),
            changed_since_manifest=recorded.get("sha256") != file_sha256(legacy),
            detail="faiss.index is used only by /ask and is not covered by the manifest",
        )
