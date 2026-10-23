"""End-to-end INR budgets through the real API on a temporary experimental catalogue.

Fixture records -> adapters -> experimental DB (v3) -> synthetic estimates -> FAISS +
manifest -> real controller (startup manifest check) -> /chat. Includes one verified
INR product (inserted directly; the Amazon data has none). The LLM provider records
every prompt and is unavailable to generation, so answers use the offline fallback.

2026-10-23: every price is INR. The fixture's USD sneaker (TST-SNK-001, USD 49.99) is
compared and shown only as its documented conversion, ₹4,153.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts import generate_embeddings as gen
from src.api import app as api_module
from src.api.app import app, set_agent_controller
from src.config.settings import PROJECT_ROOT, get_settings
from src.data.adapters.amazon_reviews_2023 import AmazonReviews2023Adapter
from src.data.adapters.pipeline import ingest, read_jsonl
from src.db.connection import get_connection
from src.db.experimental_schema import apply_experimental_v3
from src.db.migrations import run_migrations
from src.embeddings import embeddings as emb
from src.llm.provider import MockLLMProvider
from src.pricing.estimates import generate_estimates, load_range_config

FIX = PROJECT_ROOT / "tests" / "fixtures" / "adapters"
PROMPTS: list = []


def build(tmp_path: Path, with_inr: bool) -> tuple[Path, Path]:
    db = tmp_path / "exp.db"
    conn = get_connection(db)
    run_migrations(conn)
    apply_experimental_v3(conn)
    ingest(conn, AmazonReviews2023Adapter(revision="fixture"), read_jsonl(FIX / "amazon2023_meta.jsonl"),
           read_jsonl(FIX / "amazon2023_reviews.jsonl"))
    if with_inr:
        conn.execute("INSERT INTO data_sources (name, license) VALUES ('test-verified-inr', 'test fixture')")
        sid = conn.execute("SELECT id FROM data_sources WHERE name = 'test-verified-inr'").fetchone()[0]
        conn.execute("INSERT INTO products (source_id, external_id, title, description, price, currency) "
                     "VALUES (?, 'INR-1', 'Desi Canvas Sneakers', 'Test fixture sneakers priced in INR.', 2999, 'INR')",
                     (sid,))
    generate_estimates(conn, load_range_config(PROJECT_ROOT / "config" / "price_estimate_ranges.toml"))
    conn.commit()
    conn.close()
    gen.rebuild_indexes(db, tmp_path / "vs")
    return db, tmp_path / "vs"


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request):
    with_inr, enabled = request.param
    monkeypatch.setattr(emb, "check_ollama_available", lambda: False)
    for var in ("VECTOR_STORE_PATH", "PRODUCT_INDEX_PATH", "REVIEW_INDEX_PATH"):
        monkeypatch.delenv(var, raising=False)
    db, vs = build(tmp_path, with_inr)
    monkeypatch.setenv("SHELFSENSE_DB_PATH", str(db))
    monkeypatch.setenv("SHELFSENSE_VECTOR_STORE_DIR", str(vs))
    monkeypatch.setenv("SHELFSENSE_ESTIMATED_PRICES_ENABLED", "true" if enabled else "false")
    monkeypatch.setattr(api_module, "_API_KEY", None)
    PROMPTS.clear()

    def recording_provider():
        def handler(messages):
            PROMPTS.append(json.dumps(messages, ensure_ascii=False))
            return "{}"  # never valid: forces the deterministic offline fallback
        return MockLLMProvider(handler=handler, available=False)

    monkeypatch.setattr(api_module, "OllamaProvider", recording_provider)
    get_settings.cache_clear()
    set_agent_controller(None)
    with TestClient(app) as client:
        conn = sqlite3.connect(db)
        estimates = {ext: amt for ext, amt in conn.execute(
            "SELECT p.external_id, e.amount FROM price_estimates e JOIN products p ON p.id = e.product_id")}
        ids = dict(conn.execute("SELECT external_id, id FROM products"))
        conn.close()
        yield client, estimates, ids
    if api_module._agent_controller is not None:
        api_module._agent_controller.conn.close()
    set_agent_controller(None)
    get_settings.cache_clear()


def chat(client, message, **kw):
    r = client.post("/chat", json={"message": message, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def cards(body) -> dict[str, dict]:
    return {c["external_id"]: c for c in body.get("products") or []}


def no_estimate_leak(body, estimates) -> None:
    text = json.dumps({k: body.get(k) for k in ("answer", "evidence", "support_report")}, ensure_ascii=False)
    text += "".join(PROMPTS)
    for amount in estimates.values():
        n = int(amount)
        for pat in (f"{n:,}", f"{amount:.2f}", rf"\b{n}\b"):
            assert not re.search(pat, text), f"estimate {amount} leaked ({pat})"


ENABLED_INR = pytest.mark.parametrize("api", [(True, True)], indirect=True)


USD_SNEAKER_INR = 4153.0  # TST-SNK-001: USD 49.99 x 83.0815 (ECB 2023-09-29), whole rupees


@ENABLED_INR
def test_sneakers_under_inr_budget_with_estimates(api) -> None:
    client, estimates, _ = api
    budget = max(estimates["TST-SNK-002"], 2999) + 10
    body = chat(client, f"Find sneakers under ₹{budget:,.0f}.")
    got = cards(body)
    # The USD sneaker is judged on its INR conversion (₹4,153), never as "49.99 rupees".
    assert set(got) == {"TST-SNK-002", "INR-1"} | ({"TST-SNK-001"} if USD_SNEAKER_INR <= budget else set())
    est = got["TST-SNK-002"]
    assert est["price"] is None and est["currency"] is None  # source price fields unchanged
    assert est["price_status"] == "estimated"
    assert est["estimated_price"]["amount"] == estimates["TST-SNK-002"]
    assert est["estimated_price"]["label"] == "Synthetic estimate, not a retailer price"
    assert any(c["detail"].startswith("Synthetic estimate ₹") for c in est["constraint_checks"])
    assert got["INR-1"]["price_status"] == "verified" and got["INR-1"]["estimated_price"] is None
    price_item = next(i for i in body["constraints"]["items"] if i["kind"] == "price")
    assert price_item["value"]["uses_synthetic_estimates"] is True and "not retailer prices" in price_item["label"]
    assert set(body["active_filters"]) == {"category", "brand", "min_price", "max_price", "min_rating"}
    assert body["generation"]["mode"] == "none"  # product-first: the search itself calls no model
    assert not PROMPTS
    no_estimate_leak(body, estimates)
    # The optional summary of these results is the model call; estimates never reach it.
    r = client.post("/chat/summary", json={"session_id": body["session_id"],
                                           "result_product_ids": body["result_product_ids"]})
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["generation"]["mode"] == "fallback"
    assert cards(summary)["TST-SNK-002"]["estimated_price"]["amount"] == estimates["TST-SNK-002"]
    assert PROMPTS, "the provider must have received prompts, or the leak check proves nothing"
    no_estimate_leak(summary, estimates)


@ENABLED_INR
def test_budget_below_estimate_excludes_it(api) -> None:
    client, estimates, _ = api
    body = chat(client, f"Find sneakers under ₹{min(estimates['TST-SNK-002'], 2999) - 10:,.0f}.")
    assert "TST-SNK-002" not in cards(body) and "INR-1" not in cards(body)


@ENABLED_INR
def test_handbag_and_backpack_style_queries(api) -> None:
    client, estimates, _ = api
    body = chat(client, f"Find backpacks under ₹{estimates['TST-BAG-001'] + 10:,.0f}.")
    assert set(cards(body)) == {"TST-BAG-001"}
    assert cards(body)["TST-BAG-001"]["price_status"] == "estimated"
    no_estimate_leak(body, estimates)


@pytest.mark.parametrize("api", [(False, True)], indirect=True)
def test_usd_source_price_is_judged_in_inr_and_unknown_never_matches(api, tmp_path) -> None:
    client, estimates, ids = api
    # Sneakers left: TST-SNK-001 (USD 49.99 = ₹4,153) and TST-SNK-002 once its estimate is removed (unknown).
    conn = sqlite3.connect(tmp_path / "exp.db")
    conn.execute("DELETE FROM price_estimates WHERE product_id = ?", (ids["TST-SNK-002"],))
    conn.commit()
    conn.close()
    body = chat(client, "Find sneakers under ₹4,000.")
    assert cards(body) == {} and body["constraints"]["matching_products"] == 0  # ₹4,153 > ₹4,000
    body = chat(client, "Find sneakers under ₹5,000.")
    assert set(cards(body)) == {"TST-SNK-001"}
    usd = cards(body)["TST-SNK-001"]
    assert (usd["price"], usd["currency"], usd["price_status"]) == (USD_SNEAKER_INR, "INR", "converted")
    assert usd["price_conversion"]["from_currency"] == "USD" and usd["price_conversion"]["rate_date"] == "2023-09-29"
    assert "49.99" not in json.dumps(body)  # the source amount never reaches a response
    price_item = next(i for i in body["constraints"]["items"] if i["kind"] == "price")
    assert price_item["status"] == "applied" and price_item["value"]["uses_synthetic_estimates"] is True


@ENABLED_INR
def test_compare_shows_labelled_estimates(api) -> None:
    client, estimates, ids = api
    budget = max(estimates["TST-SNK-002"], 2999) + 10
    shown = chat(client, f"Find sneakers under ₹{budget:,.0f}.")
    body = chat(client, "Compare these two", session_id=shown["session_id"],
                product_ids=[ids["TST-SNK-002"], ids["INR-1"]])
    assert body["intent"] == "COMPARE"
    got = cards(body)
    assert got["TST-SNK-002"]["price_status"] == "estimated" and got["INR-1"]["price_status"] == "verified"
    no_estimate_leak(body, estimates)


@pytest.mark.parametrize("api", [(True, False)], indirect=True)
def test_disabled_estimates_use_source_prices_only(api) -> None:
    client, estimates, _ = api
    body = chat(client, "Find sneakers under ₹10,000.")
    got = cards(body)
    assert set(got) == {"INR-1", "TST-SNK-001"}  # verified INR, and the converted USD sneaker; no estimates
    price_item = next(i for i in body["constraints"]["items"] if i["kind"] == "price")
    assert price_item["value"]["uses_synthetic_estimates"] is False
    no_estimate_leak(body, estimates)


@pytest.mark.parametrize("api", [(False, False)], indirect=True)
def test_dollar_budget_asks_for_rupees_and_is_not_converted(api) -> None:
    client, estimates, _ = api
    body = chat(client, "Find sneakers under $60.")
    assert cards(body) == {}
    msg = body["clarification_question"]
    assert "rupees (₹)" in msg and "won't convert" in msg and "$" not in msg


@pytest.mark.parametrize("api", [(False, False)], indirect=True)
def test_disabled_cards_never_show_estimates(api) -> None:
    client, estimates, ids = api
    detail = client.get(f"/products/{ids['TST-SNK-002']}").json()["product"]
    assert detail["price_status"] == "unknown" and detail["estimated_price"] is None


@pytest.mark.parametrize("api", [(False, True)], indirect=True)
def test_budget_without_currency_is_rupees(api) -> None:
    client, estimates, _ = api
    body = chat(client, "Find sneakers under 5000.")
    assert body["clarification_question"] is None
    price_item = next(i for i in body["constraints"]["items"] if i["kind"] == "price")
    assert price_item["value"]["currency"] == "INR" and price_item["value"]["currency_source"] == "catalog_default"
    assert "TST-SNK-001" in cards(body)  # ₹4,153
