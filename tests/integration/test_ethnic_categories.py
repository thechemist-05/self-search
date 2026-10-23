"""Sarees, lehengas and earrings through the real pipeline (2026-10-23).

An adversarial fixture catalogue goes through the real controller: rules-first parsing,
constraint resolution, hybrid retrieval with real FAISS indexes, INR price normalisation
(USD source prices converted at the documented rate), evidence assembly and the validator.
Every shown product must meet every requirement; results are never padded.
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

# external_id -> (title, details, source price, source currency, review text)
# USD prices: 30.0 -> ₹2,492; 40.0 -> ₹3,323; 15.0 -> ₹1,246; 20.0 -> ₹1,662.
CATALOGUE: dict[str, tuple[str, dict, float | None, str | None, str]] = {
    "SAR-RED-SILK": ("Women's Red Banarasi Silk Saree with Blouse Piece", {}, 30.0, "USD", "Lovely saree, rich colour."),
    "SAR-RED-SILK-DEAR": ("Red Kanjivaram Silk Saree Wedding Collection", {}, 40.0, "USD", "Beautiful heavy saree."),
    "SAR-RED-COTTON": ("Red Cotton Saree Handloom Daily Wear", {}, 2100.0, "INR", "Soft cotton, good for daily wear."),
    "SAR-BLUE-SILK": ("Women's Blue Silk Saree Festive", {}, 2400.0, "INR", "Nice blue silk, festive look."),
    "SAR-GEORGETTE": ("Pink Georgette Party Wear Sari", {}, None, None, "Light and flowy for parties."),
    "SAR-BLOUSE": ("Readymade Red Silk Saree Blouse for Women", {}, 900.0, "INR", "Blouse fits well."),
    "SAR-PETTICOAT": ("Saree Shapewear Petticoat Cotton", {}, 500.0, "INR", "Comfortable under a saree."),
    "LEH-BRIDAL": ("Bridal Red Lehenga Choli with Dupatta Heavy Embroidery", {}, 60.0, "USD", "Stunning for my wedding."),
    "LEH-FESTIVE": ("Women's Festive Mirror Work Lehnga Choli", {}, 2800.0, "INR", "Wore it for Navratri."),
    "LEH-GHAGRA": ("Girls Ghagra Choli Set Velvet", {}, None, None, "Cute outfit for my daughter."),
    "LEH-SAREE": ("Ready to Wear Lehenga Saree Net", {}, 2500.0, "INR", "Easy to drape."),
    "EAR-BRIDAL-JHUMKA": ("Bridal Kundan Jhumka Earrings Gold Plated", {}, 1200.0, "INR", "Perfect with my bridal lehenga."),
    "EAR-BRIDAL-DEAR": ("Bridal Polki Jhumki Earrings Heavy", {}, 20.0, "USD", "Gorgeous for the wedding."),
    "EAR-OXIDISED": ("Oxidised Silver Jhumka Earrings Boho", {}, 15.0, "USD", "Light and pretty."),
    "EAR-CHANDBALI": ("Gold Plated Meenakari Chandbali Earrings", {}, 1400.0, "INR", "Colourful enamel work."),
    "EAR-STUDS": ("Pearl Stud Earrings for Women Everyday", {}, 600.0, "INR", "Simple daily studs."),
    "EAR-SET": ("Kundan Necklace with Jhumka Earrings Set Bridal", {}, 1300.0, "INR", "The set is beautiful."),
    "EAR-BACKS": ("Earring Backs Replacement Gold 100 Pieces", {}, 200.0, "INR", "Useful backs."),
    "NECKLACE": ("Gold Plated Choker Necklace for Women", {}, 1100.0, "INR", "Matches my jhumkas."),
    "POTLI": ("Embroidered Potli Bag Clutch Purse", {}, 700.0, "INR", "Goes so well with my saree and jhumkas."),
    "HEADPHONES": ("Wireless Bluetooth Headphones", {}, 25.0, "USD", "Great sound."),
}


def _build(tmp_path: Path) -> tuple[Path, Path, dict[str, int]]:
    db = tmp_path / "ethnic.db"
    conn = get_connection(db)
    run_migrations(conn)
    apply_experimental_v3(conn)
    conn.execute("INSERT INTO data_sources (name, license) VALUES ('ethnic-fixture', 'test fixture')")
    sid = conn.execute("SELECT id FROM data_sources").fetchone()[0]
    ids = {}
    for ext, (title, details, price, currency, review) in CATALOGUE.items():
        cur = conn.execute(
            "INSERT INTO products (source_id, external_id, title, price, currency, avg_rating, rating_count, "
            "attributes_json) VALUES (?, ?, ?, ?, ?, 4.3, 12, ?)",
            (sid, ext, title, price, currency, json.dumps({"store": "Fixture", "details": details, "features": []})))
        ids[ext] = cur.lastrowid
        for n in range(3):
            conn.execute("INSERT INTO reviews (product_id, source_id, external_id, rating, title, text) "
                         "VALUES (?, ?, ?, 5, 'Good', ?)", (cur.lastrowid, sid, f"{ext}-r{n}", f"{review} ({n})"))
    conn.commit()
    conn.close()
    gen.rebuild_indexes(db, tmp_path / "vs")
    return db, tmp_path / "vs", ids


@pytest.fixture(scope="module")
def ask(tmp_path_factory: pytest.TempPathFactory):
    mp = pytest.MonkeyPatch()
    mp.setattr(emb, "check_ollama_available", lambda: False)
    for var in ("VECTOR_STORE_PATH", "PRODUCT_INDEX_PATH", "REVIEW_INDEX_PATH", "SHELFSENSE_QUERY_PARSER_MODE"):
        mp.delenv(var, raising=False)
    db, vs, ids = _build(tmp_path_factory.mktemp("ethnic"))
    mp.setenv("SHELFSENSE_VECTOR_STORE_DIR", str(vs))
    get_settings.cache_clear()
    conn = open_api_connection(db)
    controller = bench.build_controller(conn, MockLLMProvider(available=False), vs)
    controller.intent_router.search_limit = 20
    by_id = {v: k for k, v in ids.items()}

    def run(text: str):
        r = controller.handle_message(text)
        return r, {by_id[i] for i in r.result_product_ids}

    yield run
    conn.close()
    mp.undo()
    get_settings.cache_clear()


@pytest.mark.parametrize("query,expected", [
    # sarees: blouses, petticoats, a lehenga-style saree and a bag whose *review* mentions a saree are not sarees
    ("saree", {"SAR-RED-SILK", "SAR-RED-SILK-DEAR", "SAR-RED-COTTON", "SAR-BLUE-SILK", "SAR-GEORGETTE"}),
    ("sari", {"SAR-RED-SILK", "SAR-RED-SILK-DEAR", "SAR-RED-COTTON", "SAR-BLUE-SILK", "SAR-GEORGETTE"}),
    ("silk sarees", {"SAR-RED-SILK", "SAR-RED-SILK-DEAR", "SAR-BLUE-SILK"}),
    ("georgette saree", {"SAR-GEORGETTE"}),
    ("banarasi saree", {"SAR-RED-SILK"}),
    # colour + material + INR budget: USD 30 = ₹2,492 qualifies; USD 40 = ₹3,323 does not; cotton is not silk
    ("red silk saree under ₹3,000", {"SAR-RED-SILK"}),
    ("red saree under ₹3000", {"SAR-RED-SILK", "SAR-RED-COTTON"}),
    # lehengas and their spellings; a lehenga-style saree is neither
    ("lehenga", {"LEH-BRIDAL", "LEH-FESTIVE", "LEH-GHAGRA"}),
    ("lehnga", {"LEH-BRIDAL", "LEH-FESTIVE", "LEH-GHAGRA"}),
    ("lehenga choli", {"LEH-BRIDAL", "LEH-FESTIVE", "LEH-GHAGRA"}),
    ("ghagra choli", {"LEH-BRIDAL", "LEH-FESTIVE", "LEH-GHAGRA"}),
    ("festive lehenga", {"LEH-FESTIVE"}),            # occasion stated by the listing
    ("bridal lehenga", {"LEH-BRIDAL"}),
    # earrings: never necklaces, sets or earring backs
    ("earrings", {"EAR-BRIDAL-JHUMKA", "EAR-BRIDAL-DEAR", "EAR-OXIDISED", "EAR-CHANDBALI", "EAR-STUDS"}),
    ("jhumkas", {"EAR-BRIDAL-JHUMKA", "EAR-BRIDAL-DEAR", "EAR-OXIDISED", "EAR-CHANDBALI", "EAR-STUDS"}),
    ("jhumki", {"EAR-BRIDAL-JHUMKA", "EAR-BRIDAL-DEAR", "EAR-OXIDISED", "EAR-CHANDBALI", "EAR-STUDS"}),
    ("oxidised silver jhumka", {"EAR-OXIDISED"}),
    ("pearl stud earrings", {"EAR-STUDS"}),
    # occasion + type + budget: USD 20 = ₹1,662 is over ₹1,500; the necklace set is not earrings
    ("bridal jhumkas under ₹1,500", {"EAR-BRIDAL-JHUMKA"}),
    ("bridal earrings", {"EAR-BRIDAL-JHUMKA", "EAR-BRIDAL-DEAR"}),
])
def test_exact_results(ask, query, expected) -> None:
    response, got = ask(query)
    assert got == expected, (query, sorted(got))


def test_no_padding_when_nothing_matches(ask) -> None:
    response, got = ask("green velvet saree under ₹500")
    assert got == set() and response.products == []
    assert response.constraints.matching_products == 0


def test_cards_show_inr_only_and_price_checks_are_inr(ask) -> None:
    response, _ = ask("red silk saree under ₹3,000")
    card = response.products[0]
    assert (card.price, card.currency, card.price_status) == (2492.0, "INR", "converted")
    assert card.price_conversion.from_currency == "USD" and card.price_conversion.rate_date == "2023-09-29"
    payload = response.model_dump_json()
    assert "$" not in payload and "30.0" not in payload.replace("2492.0", "")
    price_checks = [c.detail for c in card.constraint_checks if c.kind == "price"]
    assert price_checks and price_checks[0].startswith("₹2,492 ≤ ₹3,000")
    kinds = {c.kind for c in card.constraint_checks}
    assert {"product_type", "colour", "material", "price"} <= kinds


def test_unavailable_price_never_meets_a_budget(ask) -> None:
    _, got = ask("georgette saree under ₹100000")
    assert "SAR-GEORGETTE" not in got  # no source price and no estimate: price unavailable
    response, got = ask("georgette saree")
    assert got == {"SAR-GEORGETTE"} and response.products[0].price is None
    assert response.products[0].price_status == "unknown"


def test_occasion_is_reported_and_unknown_occasions_are_counted(ask) -> None:
    response, _ = ask("festive lehenga")
    items = {i.kind: i for i in response.constraints.items}
    assert items["occasion"].value == "festive" and items["occasion"].status == "applied"
    assert response.constraints.unknown_attribute_products == 1  # LEH-GHAGRA states no occasion
    card = response.products[0]
    assert any(c.kind == "occasion" and "festive" in c.detail for c in card.constraint_checks)
