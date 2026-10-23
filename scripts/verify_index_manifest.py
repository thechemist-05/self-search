#!/usr/bin/env python
"""Check an existing vector-store directory against its database, and optionally write its manifest.

    python scripts/verify_index_manifest.py --check            # report only, never writes
    python scripts/verify_index_manifest.py --write            # write manifest.json if every check passes

The database is opened read-only. ``--write`` adds only ``manifest.json``; index
files are never modified. Exit code 0 = consistent (and written, with --write),
1 = inconsistent (nothing written), 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config.settings import get_settings  # noqa: E402
from src.vector_store.manifest import (  # noqa: E402
    IndexManifestError,
    check_index_consistency,
    verify_manifest,
    write_manifest,
)
from src.vector_store.vector_store import vector_store_dir  # noqa: E402

UNRECORDED_EMBEDDER = "unrecorded (index built before manifests existed)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="report only")
    mode.add_argument("--write", action="store_true", help="write manifest.json if consistent")
    parser.add_argument("--db", default=None, help="database path (default: SHELFSENSE_DB_PATH)")
    parser.add_argument("--index-dir", default=None, help="index directory (default: SHELFSENSE_VECTOR_STORE_DIR)")
    parser.add_argument("--embedder", default=UNRECORDED_EMBEDDER, help="embedder name to record")
    args = parser.parse_args(argv)

    db_path = Path(args.db or get_settings().db_path)
    index_dir = Path(args.index_dir) if args.index_dir else vector_store_dir()
    if not db_path.is_file():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        report = check_index_consistency(conn, index_dir)
        print(json.dumps({"db": str(db_path), "index_dir": str(index_dir), "consistent": report.ok,
                          "problems": report.problems, "indexes": report.indexes,
                          "existing_manifest_problems": verify_manifest(conn, index_dir)}, indent=2))
        if not report.ok:
            print("INCONSISTENT: no manifest written.", file=sys.stderr)
            return 1
        if args.write:
            try:
                path = write_manifest(conn, index_dir, embedder=args.embedder, db_path=str(db_path))
            except IndexManifestError as exc:
                print(f"INCONSISTENT: no manifest written: {exc}", file=sys.stderr)
                return 1
            print(f"wrote {path}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
