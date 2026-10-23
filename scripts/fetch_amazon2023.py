#!/usr/bin/env python
"""Download pinned Amazon Reviews 2023 raw files and verify their published sha256.

    python scripts/fetch_amazon2023.py --group fashion        # 2.47 GB
    python scripts/fetch_amazon2023.py --group electronics    # 27.86 GB
    python scripts/fetch_amazon2023.py --group clothing       # 45.77 GB (sarees, lehengas, earrings, 2026-10-23)
    python scripts/fetch_amazon2023.py --group lifestyle      # 14.62 GB (beauty, handmade home goods, sports)

Files are fetched from one pinned dataset revision into
``data/raw/amazon_reviews_2023/<revision>/``. Each download goes to ``<file>.part``
(resumable) and is renamed to its final name only after its size and sha256 equal
the published values, so nothing unverified is ever parsed. A mismatch leaves the
file as ``<file>.bad`` and exits non-zero. ``SOURCE.json`` records provenance.

Rights: the dataset states no licence (audited 2026-10-21). Local academic
prototyping only; attribution to McAuley Lab (Hou et al., 2024); the raw files are
not redistributed (``data/raw/`` is git-ignored).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO = "McAuley-Lab/Amazon-Reviews-2023"
REVISION = "2b6d039ed471f2ba5fd2acb718bf33b0a7e5598e"
BASE_URL = f"https://huggingface.co/datasets/{REPO}/resolve/{REVISION}/"
LICENSE_STATUS = ("No licence stated by the dataset (audited 2026-10-21). Local academic prototyping only; "
                  "attribution to McAuley Lab (Hou et al., 2024, arXiv:2403.03952); raw files not redistributed.")
# path in the repo -> (published size in bytes, published sha256 = Hugging Face LFS oid)
FILES = {
    "raw/meta_categories/meta_Amazon_Fashion.jsonl":
        (1422365805, "629dde620a4001934a8af705f6e5514a1b2d349720c26876ee803b90051a62df"),
    "raw/review_categories/Amazon_Fashion.jsonl":
        (1051324731, "e597b3ce6cbb2f9994a32e0eee6bb04fdae50725f2d49c89f6baef1a7b986c57"),
    "raw/meta_categories/meta_Electronics.jsonl":
        (5246144134, "5eb4dabd539586c49654297319ef178b62a2fe8e43108376feda125f04455d93"),
    "raw/review_categories/Electronics.jsonl":
        (22616233652, "bf11cf9c94c23ead7d3ffb174d18231fad8fcd76e72ad167f538a5599a38de8e"),
    "raw/meta_categories/meta_Clothing_Shoes_and_Jewelry.jsonl":
        (17960532903, "e5ff908573938ee114e4f4dfcfc46799b0ead24a57a8c075fbca2e6e28d78382"),
    "raw/review_categories/Clothing_Shoes_and_Jewelry.jsonl":
        (27810080533, "150eb2a9e88f61cd5a89c5337cde437dc71308c47f22899a72d9e1dad60b7356"),
    # 2026-10-23: beauty, home and lifestyle (handmade), sports and fitness
    "raw/meta_categories/meta_All_Beauty.jsonl":
        (212990142, "1cfbdf6d2ccf9c3ac52a4508b15f31f8f340508a245b65c3e3b5f461e06e97ef"),
    "raw/review_categories/All_Beauty.jsonl":
        (326611506, "36ca7c48440a8247b2dca0174d095238422b506a8a80c114dca722b7715f1c94"),
    "raw/meta_categories/meta_Handmade_Products.jsonl":
        (399261431, "c47e8f247dab7ec1d2fac93050e79a40ad5d31c3fe073838af29694fc1690530"),
    "raw/review_categories/Handmade_Products.jsonl":
        (289025980, "7576d7743e83806d155e506a6bce13c25d9979ab13522e9a1cf2dc593144e65c"),
    "raw/meta_categories/meta_Sports_and_Outdoors.jsonl":
        (4132263769, "321aba659220c48a8dcf2a4034f1eb965fae031941d81c9b7e47811caae85db9"),
    "raw/review_categories/Sports_and_Outdoors.jsonl":
        (9256856041, "47eaab08457fd7d270613f193506c479492b696a808ae5639a949b27026a4559"),
}
GROUPS = {"fashion": [p for p in FILES if "Fashion" in p], "electronics": [p for p in FILES if "Electronics" in p],
          "clothing": [p for p in FILES if "Clothing_Shoes_and_Jewelry" in p],
          "lifestyle": [p for p in FILES if any(k in p for k in ("All_Beauty", "Handmade_Products", "Sports_and_Outdoors"))]}
DISK_MARGIN = 5 * 1024**3
CHUNK = 8 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[1] / "data" / "raw" / "amazon_reviews_2023" / REVISION


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def record(name: str, entry: dict) -> None:
    source = ROOT / "SOURCE.json"
    data = json.loads(source.read_text(encoding="utf-8")) if source.exists() else {
        "dataset": REPO, "url": f"https://huggingface.co/datasets/{REPO}", "revision": REVISION,
        "license_status": LICENSE_STATUS, "files": {}}
    data["files"][name] = entry
    tmp = source.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(source)


def fetch(name: str) -> None:
    size, published = FILES[name]
    final = ROOT / name
    if final.exists():
        print(f"{name}: already verified, skipping")
        return
    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_name(final.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    free = shutil.disk_usage(final.parent).free
    if free - (size - have) < DISK_MARGIN:
        sys.exit(f"STOP: {name} needs {(size - have) / 1e9:.2f} GB more; only {free / 1e9:.2f} GB free")
    started, last = time.monotonic(), 0
    if have < size:
        headers = {"Range": f"bytes={have}-"} if have else {}
        with requests.get(BASE_URL + name, headers=headers, stream=True, timeout=(30, 300)) as r:
            if r.status_code not in (200, 206) or (have and r.status_code != 206):
                sys.exit(f"STOP: {name}: HTTP {r.status_code} (resume from {have} not honoured?)")
            with open(part, "ab") as fh:
                for block in r.iter_content(CHUNK):
                    fh.write(block)
                    have += len(block)
                    if have - last >= 1024**3:
                        rate = (have - last) / max(time.monotonic() - started, 1e-6) / 1e6
                        print(f"{name}: {have / 1e9:.2f}/{size / 1e9:.2f} GB ({rate:.1f} MB/s)", flush=True)
                        started, last = time.monotonic(), have
    actual_size = part.stat().st_size
    actual = sha256_of(part) if actual_size == size else None
    if actual_size != size or actual != published:
        bad = final.with_name(final.name + ".bad")
        part.replace(bad)
        sys.exit(f"STOP: {name} failed verification (size {actual_size} vs {size}, sha256 {actual} vs {published}); "
                 f"kept as {bad.name}, not parsed")
    part.replace(final)
    record(name, {"bytes": size, "published_sha256": published, "verified_sha256": actual,
                  "retrieved_at": datetime.now(timezone.utc).isoformat(), "url": BASE_URL + name})
    print(f"{name}: verified sha256 {actual}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--group", choices=sorted(GROUPS), required=True)
    args = parser.parse_args()
    for name in GROUPS[args.group]:
        fetch(name)


if __name__ == "__main__":
    main()
