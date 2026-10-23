#!/usr/bin/env python
"""Validate product image URLs for a catalogue and write the image file the API serves.

    python scripts/validate_product_images.py --run-dir data/experiments/<run-id>

Reads ``product_image_candidates.json`` (each product's own listing images from the verified
source metadata: MAIN first, then up to three others) and checks URLs over HTTP:

* valid = HTTP 200, an ``image/jpeg|png|webp`` content type and at least ``MIN_BYTES`` bytes
  (smaller "images" are blank placeholders or tracking pixels);
* every URL is checked once (results are cached by URL), with short timeouts and a small
  thread pool; per product the primary is checked first and at most ``MAX_VALID`` valid
  images are kept (the first is shown, the others are fallbacks for the same product);
* a URL shared by ``SHARED_LIMIT`` or more products is never used: it is not a photo of
  any one of them (placeholders, generic banners).

Writes ``product_images.json`` (validated URLs only) and ``image_report.json`` (coverage
overall and per catalogue type, broken URLs, invalid formats, shared assets). Nothing is
downloaded to disk and no URL is invented; a product without a valid image has no entry,
so the UI shows "Image unavailable". Image content is not inspected: a valid image is the
listing's own photo, but whether it shows the product well is not checked automatically.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

PROJECT = Path(__file__).resolve().parents[1]
MIN_BYTES = 1_500
MAX_VALID = 2              # primary + one validated fallback per product
SHARED_LIMIT = 5
OK_TYPES = ("image/jpeg", "image/png", "image/webp")
TIMEOUT = (5, 10)
WORKERS = 16


def check_url(session: requests.Session, url: str) -> dict:
    """{"ok": bool, "reason": str, "type": str|None, "bytes": int|None} for one URL."""
    try:
        r = session.head(url, timeout=TIMEOUT, allow_redirects=True)
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        size = r.headers.get("Content-Length")
        if r.status_code in (403, 405) or size is None:  # some hosts refuse HEAD or omit the length
            r = session.get(url, timeout=TIMEOUT, headers={"Range": f"bytes=0-{MIN_BYTES * 2}"}, stream=True)
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            rng = r.headers.get("Content-Range", "")
            size = rng.rsplit("/", 1)[-1] if "/" in rng else r.headers.get("Content-Length")
            r.close()
        if r.status_code not in (200, 206):
            return {"ok": False, "reason": f"http_{r.status_code}", "type": ctype or None, "bytes": None}
        if ctype not in OK_TYPES:
            return {"ok": False, "reason": "invalid_format", "type": ctype or None, "bytes": None}
        n = int(size) if size and str(size).isdigit() else None
        if n is None or n < MIN_BYTES:
            return {"ok": False, "reason": "empty_or_placeholder", "type": ctype, "bytes": n}
        return {"ok": True, "reason": "ok", "type": ctype, "bytes": n}
    except requests.RequestException as exc:
        return {"ok": False, "reason": f"unreachable_{type(exc).__name__}", "type": None, "bytes": None}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=None, help="only the first N products (testing)")
    args = parser.parse_args(argv)
    run_dir = args.run_dir
    cand = json.loads((run_dir / "product_image_candidates.json").read_text(encoding="utf-8"))
    items = list(cand["images"].items())[: args.limit]

    conn = sqlite3.connect(f"file:{(run_dir / 'shelfsense.db').as_posix()}?mode=ro", uri=True)
    ext_of = dict(conn.execute("SELECT id, external_id FROM products"))
    type_of = dict(conn.execute("SELECT product_id, type_key FROM catalogue_types"))
    total_products = len(ext_of)
    conn.close()

    users = Counter(u for _, e in items for u in [e["url"], *e.get("alternates", [])])
    shared = {u for u, n in users.items() if n >= SHARED_LIMIT}

    session = requests.Session()
    session.headers["User-Agent"] = "SelfSearch-academic-prototype/1.0 (image link check)"
    adapter = requests.adapters.HTTPAdapter(pool_connections=WORKERS, pool_maxsize=WORKERS)
    session.mount("https://", adapter)
    cache: dict[str, dict] = {}
    t0 = time.monotonic()

    def validate_product(item):
        pid, entry = item
        if ext_of.get(int(pid)) != entry["external_id"]:
            return pid, [], ["product_mismatch"]
        valid, reasons = [], []
        for url in [entry["url"], *entry.get("alternates", [])]:
            if url in shared:
                reasons.append("shared_asset")
                continue
            res = cache.get(url)
            if res is None:
                res = cache[url] = check_url(session, url)
            if res["ok"]:
                valid.append(url)
                if len(valid) >= MAX_VALID:
                    break
            else:
                reasons.append(res["reason"])
        return pid, valid, reasons

    results = {}
    reasons_all = Counter()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for n, (pid, valid, reasons) in enumerate(pool.map(validate_product, items), 1):
            results[pid] = valid
            reasons_all.update(reasons)
            if n % 5000 == 0:
                print(f"[{time.strftime('%H:%M:%S')}] {n:,}/{len(items):,} products checked, "
                      f"{sum(1 for v in results.values() if v):,} with a valid image", flush=True)

    images = {pid: {"external_id": cand["images"][pid]["external_id"], "url": v[0], "alternates": v[1:]}
              for pid, v in results.items() if v}
    by_type_total = Counter(type_of.get(int(pid), "untyped") for pid in ext_of)
    by_type_ok = Counter(type_of.get(int(pid), "untyped") for pid in images)
    url_ok = sum(1 for r in cache.values() if r["ok"])
    report = {
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "seconds": round(time.monotonic() - t0),
        "rules": {"min_bytes": MIN_BYTES, "content_types": OK_TYPES, "shared_limit": SHARED_LIMIT,
                  "max_valid_per_product": MAX_VALID},
        "products": total_products,
        "products_with_valid_image": len(images),
        "products_without_valid_image": total_products - len(images),
        "coverage_pct": round(100 * len(images) / max(total_products, 1), 2),
        "coverage_by_type": {k: {"products": by_type_total[k], "with_image": by_type_ok[k],
                                 "pct": round(100 * by_type_ok[k] / by_type_total[k], 2)}
                             for k in sorted(by_type_total)},
        "products_with_fallback_image": sum(1 for e in images.values() if e["alternates"]),
        "urls_checked": len(cache), "urls_valid": url_ok, "urls_invalid": len(cache) - url_ok,
        "failure_reasons": dict(reasons_all),
        "shared_assets_skipped": len(shared),
        "duplicate_urls_kept": sum(1 for u, n in users.items() if 1 < n < SHARED_LIMIT),
        "category_check": ("not automated: each image is the listing's own photo from the source record "
                           "(exact parent_asin match); image content is not inspected"),
    }
    out = {"source": {**cand["source"], "validated": True, "validated_at": report["validated_at"],
                      "rules": report["rules"]}, "images": images}
    tmp = run_dir / "product_images.json.tmp"
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    tmp.replace(run_dir / "product_images.json")
    (run_dir / "image_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("products", "products_with_valid_image", "coverage_pct",
                                              "urls_checked", "failure_reasons")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
