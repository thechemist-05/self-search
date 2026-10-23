"""
Shared fixtures for Phase 1 tests.

Extends the base conftest with database and fixture data helpers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest


@pytest.fixture
def fixture_data() -> dict:
    """Load the sample fixture data (20 products, 100 reviews)."""
    fixture_path = Path(__file__).parent / "fixtures" / "sample_data.json"
    with open(fixture_path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def fixture_products(fixture_data: dict) -> list[dict]:
    """Return the list of fixture products."""
    return fixture_data["products"]


@pytest.fixture
def fixture_reviews(fixture_data: dict) -> list[dict]:
    """Return the list of fixture reviews."""
    return fixture_data["reviews"]


def wire_generation_mode(llm_service_mock, mode: Literal["llm", "fallback", "none"] = "llm") -> None:
    """Make a ``MagicMock(spec=LLMService)`` answer ``generate_answer_with_mode``.

    Delegates to the mock's ``generate_answer`` (so its return value and call
    assertions keep working) and reports the given generation mode.
    """
    from src.llm.schemas import GenerationInfo

    llm_service_mock.generate_answer_with_mode.side_effect = lambda *a, **kw: (
        llm_service_mock.generate_answer(*a, **kw), GenerationInfo(mode=mode),
    )


# Small 6-product catalogue shared by presentation, contract and concurrency tests:
# (id, title, brand_id, category_id, price, avg_rating)
PERSONAL_CATALOG = [
    (1, "Wireless Earbuds", 1, 1, 79.99, 4.1),
    (2, "Bluetooth Speaker", 2, 1, 45.99, 4.2),
    (3, "Hiking Backpack", 3, 2, 49.99, 4.3),
    (4, "Mechanical Keyboard", 4, 1, 69.99, 4.3),
    (5, "Noise Cancelling Headphones", 1, 1, 129.99, 4.6),
    (6, "Trail Shoes", 3, 2, 89.99, 4.5),
]
PERSONAL_BRANDS = {1: "SoundWave", 2: "BoomBox", 3: "TrailMaster", 4: "KeyCraft"}
PERSONAL_CATEGORIES = {1: "Electronics", 2: "Sports_and_Outdoors"}


def personal_catalog_products() -> list[dict]:
    """Catalogue rows shaped like retriever output, in id order."""
    return [
        {
            "id": pid, "title": title, "brand_id": b, "brand_name": PERSONAL_BRANDS[b],
            "category_id": c, "category_name": PERSONAL_CATEGORIES[c], "price": price,
            "currency": "USD",  # as stored (the schema default for these fixture rows)
            "avg_rating": rating,
        }
        for pid, title, b, c, price, rating in PERSONAL_CATALOG
    ]


@pytest.fixture
def personal_db(tmp_path: Path):
    """Temporary schema-v2 database with a 6-product catalogue and no users.

    Opened with ``check_same_thread=False`` like the API connection, so it can
    be used from FastAPI TestClient worker threads.
    """
    import sqlite3

    from src.db.migrations import run_migrations

    conn = sqlite3.connect(str(tmp_path / "personal.db"), check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = OFF")  # throwaway test DB: skip fsync per commit
    run_migrations(conn)
    conn.execute("INSERT INTO data_sources (id, name) VALUES (1, 'test-fixture')")
    conn.executemany("INSERT INTO categories (id, name) VALUES (?, ?)", PERSONAL_CATEGORIES.items())
    conn.executemany("INSERT INTO brands (id, name) VALUES (?, ?)", PERSONAL_BRANDS.items())
    conn.executemany(
        "INSERT INTO products (id, source_id, external_id, title, brand_id, category_id, price, avg_rating) "
        "VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
        [(pid, f"P{pid:03d}", t, b, c, p, r) for pid, t, b, c, p, r in PERSONAL_CATALOG],
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def catalogue_db(tmp_path: Path, fixture_data: dict) -> Path:
    """Path to a temporary schema-v2 database holding the 20-product / 100-review fixture (closed)."""
    from src.data.ingestion import ingest_from_fixture
    from src.db.connection import get_connection
    from src.db.migrations import run_migrations

    db_path = tmp_path / "catalogue.db"
    conn = get_connection(db_path)
    run_migrations(conn)
    ingest_from_fixture(conn, fixture_data["products"], fixture_data["reviews"])
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return db_path


@pytest.fixture
def experimental_conn(tmp_path: Path):
    """Open connection to a temporary experimental database at schema v3 (price_estimates)."""
    from src.db.connection import get_connection
    from src.db.experimental_schema import apply_experimental_v3
    from src.db.migrations import run_migrations

    conn = get_connection(tmp_path / "experimental.db")
    run_migrations(conn)
    apply_experimental_v3(conn)
    yield conn
    conn.close()
