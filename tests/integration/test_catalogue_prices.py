"""Prices of real records in the 50,000-product Fashion catalogue, shown in INR only.

Source prices (all USD in Amazon Reviews 2023) reach cards only as their documented INR
conversion (config/exchange_rates.toml: 83.0815 INR per USD, ECB 2023-09-29); synthetic
estimates and unavailable prices are labelled; INR budgets use the INR amounts. The
catalogue is opened read-only and never changed. Skipped when the local catalogue is not
present (it is not part of the repository).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from frontend.view_models import card_price_label, conversion_note, source_price_note
from src.agent.presentation import ProductPresenter
from src.pricing.budget import budget_decision, classify_price, load_estimates

DB = Path(__file__).resolve().parents[2] / "data" / "experiments" / "fashion-v3-20261022T085353Z" / "shelfsense.db"
pytestmark = pytest.mark.skipif(not DB.is_file(), reason="local 50,000-product catalogue not present")

USD_HANDBAG = 1274   # Keyli Women Handbags Faux Leather Hobo: source price USD 23.69 = ₹1,968
INR_HANDBAG = 4225   # MKF Handbags ~ Designer Handbag: no source price, synthetic INR estimate 1,480
UNKNOWN_BAG = 34627  # FYY handmade handbag: no source price, no estimate
USD_BELT = 8088      # KAERMU Men's Leather Ratchet Belt: source price USD 7.99 = ₹664
IDS = (USD_HANDBAG, INR_HANDBAG, UNKNOWN_BAG, USD_BELT)


@pytest.fixture(scope="module")
def conn():
    c = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    yield c
    c.close()


def _cards(conn: sqlite3.Connection, enabled: bool = True) -> dict[int, dict]:
    return {c.id: c.model_dump() for c in ProductPresenter(conn, estimates_enabled=enabled).cards(list(IDS))}


def test_source_rows_are_kept_as_stored(conn) -> None:
    rows = {r[0]: r[1:] for r in conn.execute(
        "SELECT id, price, currency FROM products WHERE id IN (?, ?, ?, ?)", IDS)}
    assert rows == {USD_HANDBAG: (23.69, "USD"), INR_HANDBAG: (None, None), UNKNOWN_BAG: (None, None),
                    USD_BELT: (7.99, "USD")}  # traceability: the original amounts are untouched


def test_cards_show_every_price_in_rupees(conn) -> None:
    cards = _cards(conn)
    usd, est, unknown, belt = cards[USD_HANDBAG], cards[INR_HANDBAG], cards[UNKNOWN_BAG], cards[USD_BELT]

    assert (usd["price_status"], usd["price"], usd["currency"]) == ("converted", 1968.0, "INR")
    assert usd["price_conversion"]["from_currency"] == "USD" and usd["price_conversion"]["rate"] == 83.0815
    assert card_price_label(usd) == "₹1,968" and source_price_note(usd) == "Converted from source price"
    assert "₹83.08 per US dollar (ECB reference rate, 2023-09-29)" in conversion_note(usd)
    assert card_price_label(belt) == "₹664"

    assert (est["price_status"], est["price"], est["currency"]) == ("estimated", None, None)
    assert est["estimated_price"]["currency"] == "INR" and card_price_label(est) == "≈ ₹1,480"

    assert (unknown["price_status"], unknown["price"]) == ("unknown", None)
    assert card_price_label(unknown) == "Price unavailable"

    payload = json.dumps(cards, ensure_ascii=False)
    assert "23.69" not in payload and "7.99" not in payload and "$" not in payload  # no source amount leaks
    for card in cards.values():
        assert "$" not in card_price_label(card, with_note=True)


def test_with_estimates_disabled(conn) -> None:
    cards = _cards(conn, enabled=False)
    assert card_price_label(cards[INR_HANDBAG]) == "Price unavailable"
    assert card_price_label(cards[USD_HANDBAG]) == "₹1,968"  # source prices are unaffected


def test_inr_budget_uses_inr_amounts(conn) -> None:
    estimates = load_estimates(conn, list(IDS))
    rows = {r[0]: r[1:] for r in conn.execute(
        "SELECT id, price, currency FROM products WHERE id IN (?, ?, ?, ?)", IDS)}

    def decide(pid: int, max_inr: float):
        view = classify_price(*rows[pid], estimates.get(pid), estimates_enabled=True)
        return budget_decision(view, budget_currency="INR", max_price=max_inr)

    assert decide(USD_HANDBAG, 2000).matches and decide(USD_HANDBAG, 2000).basis == "converted"  # ₹1,968
    assert not decide(USD_HANDBAG, 1900).matches     # never "23.69 rupees"
    assert decide(INR_HANDBAG, 2000).matches and decide(INR_HANDBAG, 2000).basis == "estimated"
    assert not decide(INR_HANDBAG, 1000).matches
    assert decide(USD_BELT, 700).matches and not decide(USD_BELT, 600).matches  # ₹664
    assert not decide(UNKNOWN_BAG, 1_00_00_000).matches and decide(UNKNOWN_BAG, 2000).reason == "price_unknown"
