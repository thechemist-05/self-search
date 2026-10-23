"""The live end-to-end checker must detect wrong cards and ungrounded evidence (no false passes)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.e2e_demo_check import Checker


def _checker(catalogue_db: Path) -> tuple[Checker, sqlite3.Connection]:
    conn = sqlite3.connect(f"file:{catalogue_db.as_posix()}?mode=ro", uri=True)
    return Checker("http://unused", conn), conn


def _card(conn: sqlite3.Connection, pid: int, **override) -> dict:
    """A card as the API sends it: the database price normalised to INR (documented conversion)."""
    from src.pricing.budget import classify_price

    price, cur, rating = conn.execute("SELECT price, currency, avg_rating FROM products WHERE id = ?", (pid,)).fetchone()
    view = classify_price(price, cur, None, estimates_enabled=False)
    conversion = {"from_currency": view.fx.currency, "rate": view.fx.rate} if view.fx else None
    return {"id": pid, "price": view.amount, "currency": view.currency, "price_status": view.status,
            "price_conversion": conversion, "catalog_rating": rating, "estimated_price": None, **override}


def test_cards_matching_the_database_pass(catalogue_db: Path) -> None:
    c, conn = _checker(catalogue_db)
    assert c.card_failures({"products": [_card(conn, 1), _card(conn, 2)]}) == []


def test_a_raw_dollar_price_or_amount_fails(catalogue_db: Path) -> None:
    c, conn = _checker(catalogue_db)
    price, cur = conn.execute("SELECT price, currency FROM products WHERE id = 1").fetchone()
    assert c.card_failures({"products": [_card(conn, 1, price=price, currency=cur)]})  # the source USD amount
    leaked = {"products": [_card(conn, 1)], "answer": {"summary": "Only $24.99 today"}}
    assert any("foreign-currency" in f for f in c.card_failures(leaked))


def test_wrong_price_rating_or_invented_estimate_fail(catalogue_db: Path) -> None:
    c, conn = _checker(catalogue_db)
    bad = [_card(conn, 1, price=1.0), _card(conn, 2, catalog_rating=1.1),
           _card(conn, 3, estimated_price={"amount": 999.0, "label": "Synthetic estimate, not a retailer price"})]
    failures = c.card_failures({"products": bad})
    assert len(failures) >= 3 and any("estimate" in f for f in failures)


def test_evidence_must_be_the_stored_review(catalogue_db: Path) -> None:
    c, conn = _checker(catalogue_db)
    rid, pid, text = conn.execute("SELECT id, product_id, text FROM reviews ORDER BY id LIMIT 1").fetchone()
    words = text.split()
    ok = {"evidence": [{"evidence_id": "R1", "review_id": rid, "product_id": pid, "text": " ".join(words[:3]) + "..."}],
          "result_product_ids": [pid], "answer": {"recommendations": [{"product_id": pid}]}}
    assert c.grounding_failures(ok) == []
    altered = {**ok, "evidence": [{**ok["evidence"][0], "text": "Invented praise"}]}
    assert any("differs" in f for f in c.grounding_failures(altered))
    wrong_product = {**ok, "evidence": [{**ok["evidence"][0], "product_id": pid + 1}]}
    assert c.grounding_failures(wrong_product)
    not_retrieved = {**ok, "answer": {"recommendations": [{"product_id": 999}]}}
    assert any("not retrieved" in f for f in c.grounding_failures(not_retrieved))
