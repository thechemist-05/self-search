"""scripts/validate_product_images.py: only validated, product-specific images are kept (offline)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts import validate_product_images as vpi


class _Resp:
    def __init__(self, status=200, ctype="image/jpeg", size="40000", rng=None):
        self.status_code, self.headers = status, {"Content-Type": ctype}
        if size is not None:
            self.headers["Content-Length"] = size
        if rng:
            self.headers["Content-Range"] = rng

    def close(self):
        pass


class _Session:
    def __init__(self, table):
        self.table, self.calls = table, []

    def head(self, url, **kw):
        self.calls.append(("HEAD", url))
        return self.table[url]

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return self.table.get(("GET", url), _Resp(200, "image/jpeg", None, "bytes 0-3000/50000"))


@pytest.mark.parametrize("resp,ok,reason", [
    (_Resp(), True, "ok"),
    (_Resp(404), False, "http_404"),
    (_Resp(ctype="text/html"), False, "invalid_format"),
    (_Resp(ctype="image/gif"), False, "invalid_format"),
    (_Resp(size="43"), False, "empty_or_placeholder"),
])
def test_check_url(resp, ok, reason) -> None:
    res = vpi.check_url(_Session({"u": resp}), "u")
    assert res["ok"] is ok and res["reason"] == reason


def test_missing_length_falls_back_to_a_ranged_get() -> None:
    s = _Session({"u": _Resp(size=None)})
    res = vpi.check_url(s, "u")
    assert res["ok"] and res["bytes"] == 50000 and ("GET", "u") in s.calls


def _setup(tmp_path: Path, images: dict) -> Path:
    db = tmp_path / "shelfsense.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, external_id TEXT)")
    conn.execute("CREATE TABLE catalogue_types (product_id INTEGER PRIMARY KEY, type_key TEXT)")
    for pid, ext, t in [(1, "A", "sarees"), (2, "B", "sarees"), (3, "C", "earrings"), (4, "D", "earrings"),
                        (5, "E", "lehengas"), (6, "F", "lehengas"), (7, "G", "lehengas"), (8, "H", "lehengas"),
                        (9, "I", "lehengas")]:
        conn.execute("INSERT INTO products VALUES (?, ?)", (pid, ext))
        conn.execute("INSERT INTO catalogue_types VALUES (?, ?)", (pid, t))
    conn.commit()
    conn.close()
    (tmp_path / "product_image_candidates.json").write_text(json.dumps({"source": {"dataset": "x"}, "images": images}),
                                                           encoding="utf-8")
    return tmp_path


def test_end_to_end_keeps_only_valid_product_specific_images(tmp_path: Path, monkeypatch) -> None:
    shared = "https://cdn/shared.jpg"
    images = {
        "1": {"external_id": "A", "url": "https://cdn/a.jpg", "alternates": ["https://cdn/a2.jpg"]},
        "2": {"external_id": "B", "url": "https://cdn/broken.jpg", "alternates": ["https://cdn/b2.jpg"]},
        "3": {"external_id": "C", "url": "https://cdn/page.html", "alternates": []},
        "4": {"external_id": "WRONG", "url": "https://cdn/d.jpg", "alternates": []},  # not this product's entry
        **{str(p): {"external_id": e, "url": shared, "alternates": []}
           for p, e in [(5, "E"), (6, "F"), (7, "G"), (8, "H"), (9, "I")]},          # one URL for 5 products
    }
    run = _setup(tmp_path, images)
    verdict = {"https://cdn/a.jpg": True, "https://cdn/a2.jpg": True, "https://cdn/broken.jpg": False,
               "https://cdn/b2.jpg": True, "https://cdn/page.html": False, "https://cdn/d.jpg": True}

    def fake_check(_session, url):
        assert url != shared, "a shared asset is never fetched or used"
        return {"ok": verdict[url], "reason": "ok" if verdict[url] else "http_404", "type": "image/jpeg", "bytes": 9999}

    monkeypatch.setattr(vpi, "check_url", fake_check)
    assert vpi.main(["--run-dir", str(run)]) == 0
    out = json.loads((run / "product_images.json").read_text(encoding="utf-8"))
    assert out["source"]["validated"] is True
    assert out["images"] == {
        "1": {"external_id": "A", "url": "https://cdn/a.jpg", "alternates": ["https://cdn/a2.jpg"]},
        "2": {"external_id": "B", "url": "https://cdn/b2.jpg", "alternates": []},   # fallback became the main image
    }
    report = json.loads((run / "image_report.json").read_text(encoding="utf-8"))
    assert report["products"] == 9 and report["products_with_valid_image"] == 2
    assert report["coverage_by_type"]["sarees"]["pct"] == 100.0
    assert report["coverage_by_type"]["earrings"]["with_image"] == 0
    assert report["coverage_by_type"]["lehengas"]["with_image"] == 0
    assert report["shared_assets_skipped"] == 1 and report["failure_reasons"]["shared_asset"] == 5
    assert report["failure_reasons"]["product_mismatch"] == 1
