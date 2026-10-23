"""Product image URLs recovered from the verified source, kept beside the database."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from scripts import extract_product_images as ex
from src.agent.presentation import ProductPresenter
from src.data.product_images import IMAGES_FILE, image_for, images_path_for, load_image_map
from src.vector_store.manifest import file_sha256

MAIN = {"variant": "MAIN", "large": "https://m.media-amazon.com/images/I/main._AC_.jpg", "thumb": "https://t/1.jpg"}
SIDE = {"variant": "PT01", "large": "https://m.media-amazon.com/images/I/side._AC_.jpg"}
GIF = {"variant": "MAIN", "large": "https://m.media-amazon.com/images/I/01RmK+J4pJL._AC_.gif"}


def test_pick_url_prefers_main_skips_placeholders_and_insecure_urls() -> None:
    assert ex.pick_url([SIDE, MAIN]) == MAIN["large"]
    assert ex.pick_url([SIDE]) == SIDE["large"]
    assert ex.pick_url([GIF, SIDE]) == SIDE["large"]  # the blank 1-pixel GIF is never a product photo
    assert ex.pick_url([GIF]) is None
    assert ex.pick_url([{"variant": "MAIN", "large": "http://insecure/a.jpg"}]) is None
    assert ex.pick_url([]) is None


def _db_with_products(path: Path, rows: list[tuple[int, str, str | None]]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, external_id TEXT, image_url TEXT)")
    conn.executemany("INSERT INTO products VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return path


def test_extract_matches_exact_parent_asin_only(tmp_path: Path) -> None:
    db = _db_with_products(tmp_path / "cat.db", [(1, "B001", None), (2, "B002", None), (3, "B003", None)])
    meta = tmp_path / "meta.jsonl"
    meta.write_text("\n".join(json.dumps(r) for r in [
        {"parent_asin": "B001", "images": [SIDE, MAIN]},
        {"parent_asin": "B002", "images": [GIF]},          # placeholder only -> no image
        {"parent_asin": "B999", "images": [MAIN]},         # not in the catalogue
        {"parent_asin": "b003", "images": [MAIN]},         # case differs: not the same id
    ]) + "\n", encoding="utf-8")
    before = file_sha256(db)
    result = ex.extract(db, meta, expected_sha=file_sha256(meta))
    assert result == {"products": 3, "images": {"1": {"external_id": "B001", "url": MAIN["large"]}}}
    assert file_sha256(db) == before  # the database is only read


def test_extract_refuses_a_file_that_does_not_match_its_verified_checksum(tmp_path: Path) -> None:
    import pytest

    db = _db_with_products(tmp_path / "cat.db", [(1, "B001", None)])
    meta = tmp_path / "meta.jsonl"
    meta.write_text(json.dumps({"parent_asin": "B001", "images": [MAIN]}) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="not using it"):
        ex.extract(db, meta, expected_sha="0" * 64)


def test_lookup_requires_the_same_product(tmp_path: Path) -> None:
    path = tmp_path / IMAGES_FILE
    path.write_text(json.dumps({"images": {
        "1": {"external_id": "B001", "url": MAIN["large"]},
        "2": {"external_id": "B002", "url": "http://insecure/a.jpg"},
    }}), encoding="utf-8")
    images = load_image_map(path)
    assert image_for(images, 1, "B001") == MAIN["large"]
    assert image_for(images, 1, "B999") is None   # row id reused by another catalogue: never attached
    assert image_for(images, 2, "B002") is None   # non-https entries are dropped
    assert image_for(images, 7, "B007") is None
    assert load_image_map(tmp_path / "missing.json") == {}
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert load_image_map(tmp_path / "bad.json") == {}
    assert images_path_for(tmp_path / "x" / "shelfsense.db") == (tmp_path / "x" / IMAGES_FILE).resolve()


def test_cards_use_recovered_images_only_where_the_database_has_none(catalogue_db: Path, tmp_path: Path) -> None:
    db = tmp_path / "run" / "shelfsense.db"
    db.parent.mkdir()
    shutil.copy(catalogue_db, db)
    conn = sqlite3.connect(db)
    ext1, ext2 = (conn.execute("SELECT external_id FROM products WHERE id = ?", (i,)).fetchone()[0] for i in (1, 2))
    stored2 = conn.execute("SELECT image_url FROM products WHERE id = 2").fetchone()[0]
    conn.execute("UPDATE products SET image_url = NULL WHERE id = 1")
    conn.commit()
    (db.parent / IMAGES_FILE).write_text(json.dumps({"images": {
        "1": {"external_id": ext1, "url": MAIN["large"]},
        "2": {"external_id": ext2, "url": SIDE["large"]},
        "3": {"external_id": "SOMETHING-ELSE", "url": SIDE["large"]},
    }}), encoding="utf-8")
    conn.execute("UPDATE products SET image_url = NULL WHERE id = 3")
    conn.commit()
    cards = {c.id: c for c in ProductPresenter(conn, estimates_enabled=False).cards([1, 2, 3])}
    conn.close()
    assert cards[1].image_url == MAIN["large"]   # recovered from the source metadata
    assert cards[2].image_url == stored2         # a stored URL always wins
    assert cards[3].image_url is None            # entry is for a different product


def test_presenter_without_an_image_file(catalogue_db: Path) -> None:
    conn = sqlite3.connect(catalogue_db)
    conn.execute("UPDATE products SET image_url = NULL WHERE id = 1")
    cards = ProductPresenter(conn, estimates_enabled=False).cards([1])
    conn.close()
    assert cards[0].image_url is None
