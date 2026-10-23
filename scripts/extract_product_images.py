#!/usr/bin/env python
"""Recover each catalogue product's own image URL from the verified Amazon Reviews 2023 metadata.

Usage:
    python scripts/extract_product_images.py --db data/experiments/<run-id>/shelfsense.db

Reads the metadata file named in the raw download's ``SOURCE.json`` (its sha256 is
verified first, unless ``--skip-verify``), matches records to products by exact
``parent_asin`` = ``products.external_id``, and writes ``product_images.json`` next to
the database (atomic replace). Per product it keeps one URL: the ``large`` image of the
``MAIN`` variant, else of the first image. Only https URLs are kept. GIFs are skipped:
in this source a ``.gif`` is Amazon's blank 1-pixel placeholder (one URL shared by 174
unrelated products), never a product photo.

The database is opened read-only and is not changed. Nothing is downloaded: the file
holds URL references only (images belong to their owners; the dataset states no
licence, so this is for local academic use; see docs/data-sources.md).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.product_images import images_path_for  # noqa: E402

RAW_ROOT = PROJECT_ROOT / "data" / "raw" / "amazon_reviews_2023"
META_KEY = "raw/meta_categories/meta_Amazon_Fashion.jsonl"


def pick_url(images: list[dict[str, Any]]) -> Optional[str]:
    """The product's main image: ``MAIN`` variant first, else the first image; ``large`` size."""
    ordered = sorted(images, key=lambda im: 0 if im.get("variant") == "MAIN" else 1)  # stable
    for im in ordered:
        for size in ("large", "hi_res", "thumb"):
            url = im.get(size)
            if isinstance(url, str) and url.startswith("https://") and not url.lower().endswith(".gif"):
                return url
    return None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def find_source(raw_root: Path) -> tuple[Path, dict[str, Any]]:
    """The verified metadata file and its SOURCE.json entry (the single downloaded revision)."""
    sources = sorted(raw_root.glob("*/SOURCE.json"))
    if len(sources) != 1:
        raise SystemExit(f"expected one downloaded revision under {raw_root}, found {len(sources)}")
    source = json.loads(sources[0].read_text(encoding="utf-8"))
    entry = source["files"][META_KEY]
    return sources[0].parent / META_KEY, {**entry, "dataset": source["dataset"], "revision": source["revision"]}


def extract(db_path: Path, meta_path: Path, expected_sha: Optional[str]) -> dict[str, Any]:
    if expected_sha is not None:
        actual = sha256(meta_path)
        if actual != expected_sha:
            raise SystemExit(f"metadata sha256 {actual} != verified {expected_sha}; not using it")
    db = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        by_asin = {ext: pid for ext, pid in db.execute("SELECT external_id, id FROM products")}
    finally:
        db.close()
    images: dict[str, dict[str, str]] = {}
    with meta_path.open(encoding="utf-8") as f:
        for line in f:
            if '"parent_asin"' not in line:
                continue
            rec = json.loads(line)
            asin = rec.get("parent_asin")
            if asin not in by_asin:
                continue
            url = pick_url(rec.get("images") or [])
            if url:
                images[str(by_asin[asin])] = {"external_id": asin, "url": url}
    return {"products": len(by_asin), "images": images}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, default=RAW_ROOT)
    parser.add_argument("--skip-verify", action="store_true", help="do not re-check the metadata sha256")
    args = parser.parse_args(argv)

    meta_path, entry = find_source(args.raw_root)
    result = extract(args.db, meta_path, None if args.skip_verify else entry["verified_sha256"])
    out = images_path_for(args.db)
    payload = {
        "source": {"dataset": entry["dataset"], "revision": entry["revision"], "file": META_KEY,
                   "sha256": entry["verified_sha256"], "rule": "images[variant=MAIN].large, else first image; .gif placeholders skipped",
                   "created_at": datetime.now(timezone.utc).isoformat(), "products": result["products"],
                   "with_image": len(result["images"])},
        "images": result["images"],
    }
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out)
    print(f"{len(result['images'])} of {result['products']} products have an image URL -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
