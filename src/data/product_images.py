"""Product image URLs kept beside a catalogue database, without changing the database.

The Amazon Reviews 2023 adapter drops the source ``images`` field, so
``products.image_url`` is empty in the Fashion catalogue. ``scripts/extract_product_images.py``
recovers each product's own image URL from the verified source metadata (exact
``parent_asin`` match) into ``product_images.json`` next to the database:

    {"source": {...}, "images": {"<product id>": {"external_id": "<parent_asin>", "url": "https://..."}}}

The API uses an entry only when ``products.image_url`` is empty **and** the entry's
``external_id`` equals the product's ``external_id``, so a file built for another
database can never attach a wrong image. URLs are only references: images are not
downloaded or stored, and the UI fetches them only when remote images are enabled.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger("shelfsense.data.product_images")

IMAGES_FILE = "product_images.json"

_CACHE: dict[str, tuple[float, dict[int, tuple[str, str]]]] = {}


def images_path_for(db_path: "str | Path") -> Path:
    """The image file that belongs to ``db_path`` (same directory)."""
    return Path(db_path).resolve().parent / IMAGES_FILE


def load_image_map(path: Path) -> dict[int, tuple[str, str]]:
    """``product id -> (external_id, url)``; empty when the file is missing or unreadable."""
    path = Path(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        images = {
            int(pid): (str(entry["external_id"]), str(entry["url"]))
            for pid, entry in raw.get("images", {}).items()
            if str(entry.get("url", "")).startswith("https://")
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        logger.warning("product_images_unreadable", path=str(path), error=str(exc))
        images = {}
    _CACHE[str(path)] = (mtime, images)
    return images


_ALT_CACHE: dict[str, tuple[float, dict[int, tuple[str, tuple[str, ...]]]]] = {}


def load_image_alternates(path: Path) -> dict[int, tuple[str, tuple[str, ...]]]:
    """``product id -> (external_id, fallback URLs)`` from validated files (``alternates``).

    Fallbacks are other validated photos of the same product, tried when the main one fails.
    """
    path = Path(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _ALT_CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        alts = {
            int(pid): (str(entry["external_id"]),
                       tuple(u for u in entry.get("alternates") or [] if str(u).startswith("https://")))
            for pid, entry in raw.get("images", {}).items()
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        alts = {}
    _ALT_CACHE[str(path)] = (mtime, alts)
    return alts


def alternates_for(alts: dict[int, tuple[str, tuple[str, ...]]], product_id: int,
                   external_id: Optional[str]) -> list[str]:
    """Fallback image URLs for this exact product (never another product's)."""
    entry = alts.get(product_id)
    if entry is None or not external_id or entry[0] != external_id:
        return []
    return list(entry[1])


def image_for(images: dict[int, tuple[str, str]], product_id: int, external_id: Optional[str]) -> Optional[str]:
    """The product's image URL, only if the file's entry is for this exact product."""
    entry = images.get(product_id)
    if entry is None or not external_id or entry[0] != external_id:
        return None
    return entry[1]
