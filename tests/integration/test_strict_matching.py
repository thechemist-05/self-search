"""Strict product matching through the real pipeline (2026-10-22).

An adversarial catalogue (near-miss titles, accessories, unstated colours) goes through
the real controller: rules-first parsing, constraint resolution, hybrid retrieval with
real FAISS indexes, evidence assembly and the validator. The model is unavailable, so
answers use the deterministic fallback; only the *returned products* are judged.

Every shown product must satisfy every requirement; results are never padded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import benchmark_llm as bench
from scripts import generate_embeddings as gen
from src.api.app import open_api_connection
from src.config.settings import get_settings
from src.db.connection import get_connection
from src.db.experimental_schema import apply_experimental_v3
from src.db.migrations import run_migrations
from src.embeddings import embeddings as emb
from src.llm.provider import MockLLMProvider

# external_id -> (title, store, details, est. INR or None, USD price or None)
CATALOGUE: dict[str, tuple[str, str, dict, float | None, float | None]] = {
    "H-BLK": ("Women's Black Stiletto Pointed Toe High Heels Pumps", "Lumi", {}, 2400.0, None),
    "H-WHT": ("Women's White Block Heel Pumps Wedding Shoes", "Lumi", {}, 2600.0, None),
    "H-NUDE": ("Women's Kitten Heel Pumps Office Shoes", "Lumi", {"Color": "Black"}, 1900.0, None),
    "BOOT-HEEL": ("Women's Black Chunky Heel Ankle Boots", "Lumi", {}, 3500.0, None),
    "GRIPS": ("Heel Grips for Loose Shoes Black Cushion Inserts", "FixIt", {}, None, None),
    "BAG-BLK": ("Women's Black Leather Crossbody Handbag", "Kora", {}, 2200.0, None),
    "BAG-FAUX": ("Women's Black Faux Leather Tote Handbag", "Kora", {}, 1500.0, None),
    "BAG-RED": ("Women's Red Shoulder Bag Purse", "Kora", {}, 1800.0, None),
    "RUN-1": ("Men's Running Shoes Lightweight Mesh Sneakers", "Stride", {}, 2900.0, None),
    "RUN-NIKE": ("Nike Women's Black Running Shoes Revolution", "Nike", {}, None, 54.0),
    "RUN-PINK": ("Women's Pink Running Shoes Breathable", "Nike", {}, 3100.0, None),
    "NIKE-TRAIN": ("Men's Training Sneakers Cross Trainer", "Nike", {}, 3300.0, None),  # Nike, not running
    "FOOTBALL": ("adidas Men's Football Shoe Firm Ground Cleats", "adidas", {}, None, 70.0),
    "CLEANER": ("Sneaker Cleaner Kit for Running Shoes and Sneakers", "SneakClean", {}, None, None),
    "SANDAL": ("Women's Black Slide Sandals Comfort", "Stride", {}, 900.0, None),
    "DRESS-RED": ("Women's Red Floral Summer Dress Sleeveless", "Bloom", {}, 1700.0, None),
    "DRESS-RED2": ("Women's Burgundy Wrap Midi Dress", "Bloom", {}, 2500.0, None),
    "DRESS-PLAIN": ("Women's Summer Maxi Dress with Pockets", "Bloom", {}, 1600.0, None),
    "DRESS-SHOES": ("Red Dress Shoes for Women Pointed Toe", "Lumi", {}, 2000.0, None),
    "SHIRT": ("Men's Slim Fit Red Dress Shirt", "Tailor", {}, None, 25.0),
}
REVIEW = "Comfortable and looks exactly like the photos, I would buy it again."


def _build(tmp_path: Path) -> tuple[Path, Path, dict[str, int]]:
    db = tmp_path / "strict.db"
    conn = get_connection(db)
    run_migrations(conn)
    apply_experimental_v3(conn)
    conn.execute("INSERT INTO data_sources (name, license) VALUES ('strict-fixture', 'test fixture')")
    sid = conn.execute("SELECT id FROM data_sources").fetchone()[0]
    ids = {}
    for ext, (title, store, details, est, usd) in CATALOGUE.items():
        cur = conn.execute(
            "INSERT INTO products (source_id, external_id, title, price, currency, avg_rating, rating_count, "
            "attributes_json) VALUES (?, ?, ?, ?, ?, 4.2, 10, ?)",
            (sid, ext, title, usd, "USD" if usd else None,
             json.dumps({"store": store, "details": details, "features": []})))
        ids[ext] = cur.lastrowid
        for n in range(2):
            conn.execute("INSERT INTO reviews (product_id, source_id, external_id, rating, title, text) "
                         "VALUES (?, ?, ?, 5, 'Good', ?)", (cur.lastrowid, sid, f"{ext}-r{n}", f"{REVIEW} ({ext} {n})"))
        if est is not None:
            conn.execute(
                "INSERT INTO price_estimates (product_id, currency, amount, range_min, range_max, rule_key, method, "
                "config_version, config_sha256, label) VALUES (?, 'INR', ?, 100, 10000, 'fixture', 'fixture', "
                "'fixture-v1', 'x', 'synthetic_estimate')", (cur.lastrowid, est))
    conn.commit()
    conn.close()
    gen.rebuild_indexes(db, tmp_path / "vs")
    return db, tmp_path / "vs", ids


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory: pytest.TempPathFactory):
    mp = pytest.MonkeyPatch()
    mp.setattr(emb, "check_ollama_available", lambda: False)
    for var in ("VECTOR_STORE_PATH", "PRODUCT_INDEX_PATH", "REVIEW_INDEX_PATH", "SHELFSENSE_QUERY_PARSER_MODE"):
        mp.delenv(var, raising=False)
    db, vs, ids = _build(tmp_path_factory.mktemp("strict"))
    mp.setenv("SHELFSENSE_VECTOR_STORE_DIR", str(vs))
    mp.setenv("SHELFSENSE_ESTIMATED_PRICES_ENABLED", "true")
    get_settings.cache_clear()
    conn = open_api_connection(db)
    controller = bench.build_controller(conn, MockLLMProvider(available=False), vs)
    by_id = {v: k for k, v in ids.items()}

    def ask(text: str, session_id=None):
        r = controller.handle_message(text, session_id=session_id)
        return r, [by_id[i] for i in r.result_product_ids]

    ask.conn = conn  # type: ignore[attr-defined]
    yield ask
    conn.close()
    mp.undo()
    get_settings.cache_clear()


@pytest.mark.parametrize("query,expected", [
    # colour + type: white heels, heeled boots and heel grips never qualify
    ("black heels", {"H-BLK", "H-NUDE"}),       # H-NUDE: colour from listing details, not the title
    ("white heels", {"H-WHT"}),
    # type separation: a black handbag is not a black shoe, and vice versa
    ("black handbag", {"BAG-BLK", "BAG-FAUX"}),
    ("black leather handbag", {"BAG-BLK"}),     # faux leather is not leather
    ("faux leather handbag", {"BAG-FAUX"}),
    # running shoes vs football shoes and shoe cleaners
    ("running shoes", {"RUN-1", "RUN-NIKE", "RUN-PINK"}),
    ("football boots", {"FOOTBALL"}),
    # red dresses: burgundy is a red; red dress shoes and dress shirts are not dresses
    ("red dress", {"DRESS-RED", "DRESS-RED2"}),
    ("red summer dress under ₹2000", {"DRESS-RED"}),   # type + colour + INR estimate budget
    # brand: from the store name or the start of the title
    ("nike running shoes", {"RUN-NIKE", "RUN-PINK"}),
    ("running shoes, not nike", {"RUN-1"}),
    # exclusions and colour together
    ("running shoes without pink", {"RUN-1", "RUN-NIKE"}),
    ("black running shoes", {"RUN-NIKE"}),
])
def test_only_exact_matches_are_returned(pipeline, query, expected) -> None:
    _, shown = pipeline(query)
    assert set(shown) == expected, (query, shown)
    assert len(shown) == len(set(shown))  # no duplicates


def test_no_exact_match_is_an_empty_result_not_near_misses(pipeline) -> None:
    r, shown = pipeline("green heels")
    assert shown == []
    assert r.generation.mode == "none"  # nothing to describe, so no model call
    assert r.constraints.matching_products == 0


def test_unstated_colour_is_unknown_and_counted(pipeline) -> None:
    r, shown = pipeline("blue dress")
    assert shown == []
    # three dresses meet every other requirement; only one states a colour (red), two don't
    assert r.constraints.unknown_attribute_products == 1
    colour = next(i for i in r.constraints.items if i.kind == "colour")
    assert colour.value == "blue" and "stated by the listing" in colour.label


def test_small_result_sets_are_not_padded(pipeline) -> None:
    _, shown = pipeline("white heels")
    assert shown == ["H-WHT"]


def test_cards_explain_why_each_product_qualifies(pipeline) -> None:
    r, _ = pipeline("black heels")
    checks = {c.id: {k.kind: k.detail for k in c.constraint_checks} for c in r.products}
    by_title = {c.title: c.id for c in r.products}
    nude = by_title["Women's Kitten Heel Pumps Office Shoes"]
    assert checks[nude]["colour"] == "Colour: black (stated in the listing details)"
    assert checks[by_title["Women's Black Stiletto Pointed Toe High Heels Pumps"]]["product_type"].startswith("heels:")


def test_evidence_and_recommendations_stay_on_shown_products(pipeline) -> None:
    r, _ = pipeline("black handbag")
    shown = set(r.result_product_ids)
    assert {e.product_id for e in r.evidence} <= shown
    assert {rec["product_id"] for rec in r.answer["recommendations"]} <= shown
    assert r.evidence
    for e in r.evidence:  # every snippet is the stored text of a review of the product it is attached to
        product_id, text = pipeline.conn.execute(
            "SELECT product_id, text FROM reviews WHERE id = ?", (e.review_id,)).fetchone()
        assert (product_id, text) == (e.product_id, e.text)


def test_refinement_keeps_type_and_changes_colour(pipeline) -> None:
    r1, shown1 = pipeline("black heels")
    assert set(shown1) == {"H-BLK", "H-NUDE"}
    r2, shown2 = pipeline("only white ones", r1.session_id)
    assert set(shown2) == {"H-WHT"}
