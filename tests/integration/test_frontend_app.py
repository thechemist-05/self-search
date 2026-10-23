"""
Headless UI tests for the Phase 9 Streamlit app (streamlit.testing.v1.AppTest).

The real app script runs against a recording fake API client, so every test is
offline and deterministic. Covers card display, evidence/validation display,
error and offline states, images, compare, ask-about, filter chips, price
status labels, the absence of profile controls and of a sidebar, and
accessibility labels. Messages are typed into the single search box.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Optional

import pytest
from streamlit.testing.v1 import AppTest

from frontend.api_client import ApiError
from frontend.config import FrontendConfig

APP = str(Path(__file__).resolve().parents[2] / "frontend" / "app.py")
SECRET = "SECRET-KEY-do-not-render-1234"

CARD3 = {
    "id": 3, "external_id": "P003", "title": "Hiking Backpack", "brand": "TrailMaster",
    "category": "Sports_and_Outdoors", "price": 4153.0, "currency": "INR", "price_status": "converted",
    "price_conversion": {"from_currency": "USD", "rate": 83.0815, "rate_date": "2023-09-29",
                         "source": "European Central Bank euro foreign exchange reference rates",
                         "source_url": "https://data-api.ecb.europa.eu/", "fx_version": "fx-ecb-20230929-v1"},
    "catalog_rating": 4.3,
    "catalog_rating_count": 1250, "local_review_count": 2, "image_url": "https://example.com/img/b.jpg",
    "features": ["Waterproof", "40L"], "attributes": {"weight": "0.8 kg"}, "aspects": [],
    "recommended": True, "retrieval_rank": 2,
}
CARD1 = {**CARD3, "id": 1, "external_id": "P001", "title": "Wireless Earbuds", "brand": "SoundWave",
         "category": "Electronics", "price": 6646.0, "recommended": False, "retrieval_rank": 1,
         "image_url": "https://cdn.shop.com/e.jpg", "features": []}

SEARCH = {
    "session_id": "s1", "intent": "PRODUCT_SEARCH",
    "answer": {
        "summary": "The backpack costs $1 and is great",
        "recommendations": [{"product_id": 3, "title": "LLM INVENTED TITLE", "why_it_matches": [
            {"claim": "Very light and waterproof", "evidence_ids": ["R1"]},
            {"claim": "[UNVERIFIED] It costs only $1", "evidence_ids": ["R99"]},
        ], "watch_outs": []}],
        "insufficient_evidence": False, "missing_information": None,
    },
    "clarification_question": None, "smalltalk_response": None,
    "support_report": {
        "total_claims": 2, "supported_claims": 1, "unsupported_claims": 1, "support_rate": 0.5,
        "is_valid": False, "fabricated_citations": 1, "numeric_mismatches": 0, "cross_product_mismatches": 0,
        "claim_results": [
            {"claim_text": "Very light and waterproof", "product_id": 3, "status": "SUPPORTED"},
            {"claim_text": "It costs only $1", "product_id": 3, "status": "FABRICATED_CITATION"},
        ],
    },
    "active_filters": {"category": None, "brand": None, "min_price": None, "max_price": 60.0, "min_rating": None},
    "shown_product_ids": [1, 3], "parsing_confidence": 0.4,
    "result_product_ids": [3, 1], "products": [CARD3, CARD1],
    "evidence": [{"evidence_id": "R1", "review_id": 11, "product_id": 3, "title": "Light",
                  "text": "So light, kept my gear dry", "rating": 5.0, "verified": True, "helpful_votes": 4}],
    "preference_suggestions": [], "generation": {"mode": "llm", "reason": None, "model": "m", "attempts": 1},
}


class FakeClient:
    """Records every call; returns canned responses."""

    def __init__(self, chat_responses: Optional[list[Any]] = None, health: Optional[dict] = None) -> None:
        self.calls: list[tuple] = []
        self.chat_responses = list(chat_responses or [SEARCH])
        self.health_value = health if health is not None else {
            "status": "ok", "database": "connected", "ollama": "available", "active_sessions": 0}
        self.image: Optional[tuple[bytes, str]] = (b"\x89PNG", "image/png")

    def _rec(self, *call: Any) -> None:
        self.calls.append(call)

    def named(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]

    def health(self) -> dict:
        if self.health_value == "down":
            raise ApiError("unreachable", "down")
        return self.health_value

    def chat(self, message, session_id=None, product_ids=None):
        self._rec("chat", message, session_id, product_ids)
        nxt = self.chat_responses.pop(0) if len(self.chat_responses) > 1 else self.chat_responses[0]
        if isinstance(nxt, Exception):
            raise nxt
        return copy.deepcopy(nxt)

    def product(self, pid):
        self._rec("product", pid)
        return {"product": CARD3, "reviews": [{"review_id": 11, "rating": 5.0, "title": "t", "text": "Great $5 bag",
                                               "review_date": "2024-01-01", "helpful_votes": 4, "verified": True}]}

    def clear_filter(self, sid, name):
        self._rec("clear_filter", sid, name)
        return {"session_id": sid, "active_filters": {}}

    def fetch_image(self, url):
        self._rec("fetch_image", url)
        return self.image


def _app(client: FakeClient, load_remote: bool = False) -> AppTest:
    from frontend.components.turn import clear_image_cache

    clear_image_cache()
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["client"] = client
    at.session_state["config"] = FrontendConfig(api_key=SECRET, load_remote_images=load_remote)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _say(at: AppTest, text: str) -> AppTest:
    # Form values are committed together with the submit click (as in a browser).
    at.text_input(key="hero-query").input(text)
    at.button(key="FormSubmitter:hero-search-Search").click().run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _text(at: AppTest) -> str:
    parts = []
    for kind in ("markdown", "caption", "info", "warning", "error", "success", "title", "header", "subheader"):
        parts += [str(e.value) for e in getattr(at, kind)]
    parts += [str(e.value) for e in at.text_input] + [b.label for b in at.button]
    parts += [str(t.value) for t in at.table]
    return "\n".join(parts)


class TestSearchDisplay:
    def test_cards_show_database_values_not_llm_values(self) -> None:
        text = _text(_say(_app(FakeClient()), "backpack"))
        assert "Hiking Backpack" in text and "Wireless Earbuds" in text
        assert "LLM INVENTED TITLE" not in text
        assert "₹4,153" in text and "Converted from source price" in text
        assert "4.3★ · 1,250 ratings · 2 reviews" in text
        assert text.count("Top pick") == 1  # only the recommended card is badged
        assert "Also matched your request" not in text and "Sports and Outdoors" not in text

    def test_claim_statuses_summary_label_and_validation_banner(self) -> None:
        at = _say(_app(FakeClient()), "backpack")
        text = _text(at)
        assert "AI summary" in text
        assert "Supported by cited reviews" in text and "Backed by reviews" in text
        assert "Cited evidence not found" in text and "Not verified" in text
        assert "[UNVERIFIED]" not in text  # prefix replaced by an explicit status label
        errors = [e.value for e in at.error]
        assert any(e.startswith("Only 1 of 2 claims are backed by the cited reviews") for e in errors)

    def test_evidence_and_missing_citation_in_why_panel(self) -> None:
        text = _text(_say(_app(FakeClient()), "backpack"))
        assert "So light, kept my gear dry" in text
        assert "cited review not found" in text
        # No currency-blind budget line: active_filters has no currency, so a source price is never
        # set against it here (the API's constraint checks, which know both currencies, cover prices).
        assert "Matches: Price" not in text
        assert "Aspect analysis" not in text and "stored locally" not in text  # internal details removed

    def test_valid_report_has_no_banner(self) -> None:
        ok = copy.deepcopy(SEARCH)
        ok["support_report"]["is_valid"] = True
        at = _say(_app(FakeClient([ok])), "backpack")
        assert not [e for e in at.error if "backed by the cited reviews" in str(e.value)]

    def test_insufficient_evidence_and_empty_results(self) -> None:
        empty = copy.deepcopy(SEARCH)
        empty.update(answer={"summary": "No matching products were found.", "recommendations": [],
                             "insufficient_evidence": True, "missing_information": "No products in the catalogue"},
                     products=[], result_product_ids=[], evidence=[],
                     generation={"mode": "none", "reason": "no_products_retrieved"})
        empty["support_report"].update(is_valid=True, claim_results=[])
        at = _say(_app(FakeClient([empty])), "unicorn saddle")
        assert any("Not enough evidence for a reliable recommendation" in w.value for w in at.warning)
        assert any("No products match" in i.value for i in at.info)

    def test_fallback_generation_warning(self) -> None:
        fb = copy.deepcopy(SEARCH)
        fb["generation"] = {"mode": "fallback", "reason": "provider_error"}
        at = _say(_app(FakeClient([fb])), "backpack")
        assert any("Quick answer: the AI model did not answer in time" in w.value for w in at.warning)

    def test_compare_table(self) -> None:
        cmp = copy.deepcopy(SEARCH)
        cmp["intent"] = "COMPARE"
        at = _say(_app(FakeClient([cmp])), "compare")
        assert len(at.table) == 1


class TestStates:
    def test_offline_model_banner(self) -> None:
        at = _app(FakeClient(health={"status": "ok", "database": "connected", "ollama": "unavailable"}))
        assert any("AI model is offline" in w.value for w in at.warning)

    def test_api_unreachable(self) -> None:
        at = _app(FakeClient(health="down"))
        assert any("not reachable" in e.value for e in at.error)
        assert at.button(key="FormSubmitter:hero-search-Search").disabled

    @pytest.mark.parametrize("err,expected", [
        (ApiError("unreachable", "x"), "not reachable"),
        (ApiError("validation", "Products [9] were not shown in this session", 422), "were not shown"),
        (ApiError("server", "boom", 500), "server hit an error"),
    ])
    def test_chat_errors_are_shown(self, err, expected) -> None:
        at = _say(_app(FakeClient([err])), "backpack")
        assert any(expected in e.value for e in at.error)

    def test_conflict_starts_new_conversation(self) -> None:
        client = FakeClient([SEARCH, ApiError("conflict", "belongs to a different user", 409), SEARCH])
        at = _say(_say(_app(client), "a"), "b")
        assert at.session_state["session_id"] is None
        _say(at, "c")
        assert client.named("chat")[-1][2] is None


class TestImages:
    def test_placeholder_when_remote_disabled(self) -> None:
        client = FakeClient()
        text = _text(_say(_app(client), "backpack"))
        assert 'alt="Image unavailable for Hiking Backpack"' in text
        assert 'alt="Image unavailable for Wireless Earbuds"' in text
        assert client.named("fetch_image") == []

    def test_remote_enabled_skips_reserved_domains_and_handles_failure(self) -> None:
        client = FakeClient()
        text = _text(_say(_app(client, load_remote=True), "backpack"))
        assert [c[1] for c in client.named("fetch_image")] == ["https://cdn.shop.com/e.jpg"]  # not example.com
        assert 'alt="Product image of Wireless Earbuds"' in text
        assert 'alt="Image unavailable for Hiking Backpack"' in text

    def test_remote_failure_falls_back_to_placeholder(self) -> None:
        client = FakeClient()
        client.image = None  # e.g. offline, 404, or not an image
        text = _text(_say(_app(client, load_remote=True), "earbuds"))
        assert client.named("fetch_image")
        assert 'alt="Image unavailable for Wireless Earbuds"' in text


class TestSelectionAndFilters:
    def test_compare_selected_sends_product_ids(self) -> None:
        client = FakeClient()
        at = _say(_app(client), "backpack")
        at.checkbox(key="cmp-1-3").check().run()
        at.checkbox(key="cmp-1-1").check().run()
        at.button(key="compare-selected").click().run()
        assert client.named("chat")[-1][3] == [3, 1]

    def test_ask_about_sends_single_product_id(self) -> None:
        client = FakeClient()
        at = _say(_app(client), "backpack")
        at.button(key="ask-1-3").click().run()
        assert "Asking about <b>Hiking Backpack</b>" in _text(at)
        assert at.text_input(key="hero-query").placeholder.startswith("Ask about Hiking Backpack")
        _say(at, "is it durable?")
        assert client.named("chat")[-1][1:] == ("is it durable?", "s1", [3])

    def test_ask_about_moves_user_to_search_box_once_per_click(self) -> None:
        at = _say(_app(FakeClient()), "backpack")
        at.button(key="ask-1-3").click().run()
        assert at.session_state["focus_request"] == at.session_state["focus_done"] == 1
        assert any("hero-query" in h.proto.body and "focus(" in h.proto.body for h in at.get("html"))
        at.run()  # an unrelated rerun must not jump the page again
        assert not any("hero-query" in h.proto.body for h in at.get("html"))
        at.button(key="ask-1-1").click().run()
        assert at.session_state["focus_done"] == 2

    def test_filter_chip_clears_filter(self) -> None:
        r = _with_constraints(SEARCH, [PRICE_6000])
        client = FakeClient([r])
        at = _say(_app(client), "backpack under ₹6000")
        assert at.button(key="chip-max_price").label == "Under ₹6,000 ✕"
        at.button(key="chip-max_price").click().run()
        assert client.named("clear_filter") == [("clear_filter", "s1", "max_price")]
        assert not [b for b in at.button if (b.key or "").startswith("chip-")]  # chip gone, row hidden

    def test_range_chip_clears_both_bounds(self) -> None:
        r = _with_constraints(SEARCH, [{"kind": "price", "label": "Price: INR 500.00 – INR 800.00", "status": "applied",
                                        "value": {"min": 500.0, "max": 800.0, "currency": "INR"}}])
        client = FakeClient([r])
        at = _say(_app(client), "sandals between ₹500 and ₹800")
        at.button(key="chip-min_price-max_price").click().run()
        assert [c[2] for c in client.named("clear_filter")] == ["min_price", "max_price"]


class TestAccessibilityAndSecrets:
    def test_buttons_have_text_labels_and_images_have_alt(self) -> None:
        at = _say(_app(FakeClient()), "backpack")
        assert all(b.label and b.label.strip() for b in at.button)
        imgs = [m.value for m in at.markdown if "<img" in str(m.value)]
        assert imgs and all('alt="' in v for v in imgs)

    def test_api_key_never_rendered(self) -> None:
        at = _say(_app(FakeClient()), "backpack")
        assert SECRET not in _text(at)
        assert SECRET not in str([e.value for e in at.markdown])

    def test_disclaimer_and_review_text_escaped(self) -> None:
        at = _say(_app(FakeClient()), "backpack")
        text = _text(at)
        assert text.count("not affiliated with any retailer") == 1  # one small footer disclaimer
        # A dollar amount in AI text is never shown (the API removes it; the page guards too).
        assert "costs [amount removed: prices are shown in ₹]" in text and "&#36;1" not in text and "costs $1" not in text


# ---------------------------------------------------------------------------
# Relevance fix and redesign (Phase 9)
# ---------------------------------------------------------------------------

PRICE_6000 = {"kind": "price", "label": "Max price: ₹6,000", "status": "applied",
              "value": {"min": None, "max": 6000.0, "currency": "INR"}}


def _with_constraints(base: dict, items: list, matching: int = 1) -> dict:
    r = copy.deepcopy(base)
    r["constraints"] = {"items": items, "matching_products": matching, "catalog_currencies": ["INR"]}
    return r


CURRENCY_CLARIFY = {
    **{k: v for k, v in SEARCH.items()},
    "answer": None, "support_report": None, "products": [], "result_product_ids": [], "evidence": [],
    "generation": None,
    "clarification_question": "Self Search shows every price in Indian rupees (₹), so I can't apply a budget in "
                              "USD, and I won't convert it. Please tell me your budget in ₹.",
    "constraints": {"items": [
        {"kind": "product_type", "label": "Product type: footwear", "status": "applied", "value": ["footwear"]},
        {"kind": "price", "label": "Budget not in rupees", "status": "not_applied",
         "reason": "Your budget is in USD, but Self Search shows every price in Indian rupees (₹).",
         "value": {"min": None, "max": 60.0, "currency": "USD"}},
    ], "matching_products": 1, "catalog_currencies": ["INR"]},
}


class TestRedesign:
    def test_empty_state_hero_search_and_examples(self) -> None:
        client = FakeClient()
        at = _app(client)
        text = _text(at)
        assert "Find exactly what you have in mind." in text  # one-sentence hero, before the first search
        for gone in ("evidence first", "validation status", "meet every applied requirement", "research prototype ·"):
            assert gone not in text.replace("Academic research prototype ·", "")
        assert '<style>' in str([m.value for m in at.markdown])
        # Form values are committed together with the submit click (as in a browser).
        at.text_input(key="hero-query").input("shoes under $100")
        at.button(key="FormSubmitter:hero-search-Search").click().run()
        assert client.named("chat")[-1][1] == "shoes under $100"
        at2 = _app(FakeClient())
        at2.button(key="example-0").click().run()
        assert at2.session_state["messages"][0]["text"] == "running shoes under ₹3000"

    def test_currency_clarification_shows_why_and_no_products(self) -> None:
        at = _say(_app(FakeClient([CURRENCY_CLARIFY])), "shoe under $60")
        text = _text(at)
        assert any("rupees (₹)" in i.value for i in at.info)
        assert any(w.value.startswith("Not applied: Budget not in rupees.") for w in at.warning)
        assert "60.00" not in text and "USD 60" not in text
        assert '<span class="ss-filter">Footwear</span>' in text  # applied type is a chip
        assert "meets every applied requirement" not in text
        assert "AI summary" not in text and 'alt="' not in text  # no answer, no cards

    def test_requirements_panel_and_card_checks(self) -> None:
        r = _with_constraints(SEARCH, [
            {"kind": "product_type", "label": "Product type: backpack", "status": "applied", "value": ["backpack"]},
            PRICE_6000,
        ])
        detail = "₹4,153 ≤ ₹6,000 (converted from the source price at ₹83.08 per US dollar (ECB reference rate, 2023-09-29))"
        r["products"][0]["constraint_checks"] = [
            {"kind": "product_type", "detail": "backpack: title mentions “backpack”"},
            {"kind": "price", "detail": detail},
        ]
        at = _say(_app(FakeClient([r])), "backpack under ₹6000")
        text = _text(at)
        assert '<span class="ss-filter">Backpack</span>' in text
        assert [b.label for b in at.button if (b.key or "").startswith("chip-")] == ["Under ₹6,000 ✕"]
        assert "Matches: backpack: title mentions “backpack” · ₹4,153 ≤ ₹6,000" in text
        assert "Meets your requirements" not in text and "each meets every requirement" not in text

    def test_no_match_state(self) -> None:
        empty = _with_constraints(SEARCH, [
            {"kind": "product_type", "label": "Product type: footwear", "status": "applied"}], matching=0)
        empty.update(products=[], result_product_ids=[], evidence=[],
                     answer={"summary": "No matching products were found.", "recommendations": [],
                             "insufficient_evidence": True, "missing_information": "No products matching"})
        empty["support_report"].update(is_valid=True, claim_results=[])
        at = _say(_app(FakeClient([empty])), "sneakers under $50")
        assert any("No products match" in i.value for i in at.info)
        assert "catalogue products meet" not in _text(at)

    def test_model_status_is_explicit(self) -> None:
        at = _app(FakeClient(health={"status": "ok", "database": "connected", "ollama": "unavailable"}))
        assert "The AI model is offline, so answers are built directly from the reviews." in _text(at)


class TestControlCoverage:
    """Every interactive control has an interaction test (added before the Stage 1 restyle)."""

    def test_cancel_ask_about(self) -> None:
        at = _say(_app(FakeClient()), "backpack")
        at.button(key="ask-1-3").click().run()
        at.button(key="cancel-ask").click().run()
        assert at.session_state["ask_about"] is None
        assert "Asking about" not in _text(at)

    def test_load_stored_reviews(self) -> None:
        client = FakeClient()
        at = _say(_app(client), "backpack")
        at.button(key="detail-1-3").click().run()
        assert client.named("product") == [("product", 3)]
        assert "Great \\[amount removed: prices are shown in ₹\\] bag" in _text(at)  # review quote: no dollar amount
        assert "$5" not in _text(at)

EST = {"amount": 2340.0, "currency": "INR", "range_min": 1500.0, "range_max": 6000.0, "rule": "sneakers",
       "config_version": "illustrative-inr-v1", "label": "Synthetic estimate, not a retailer price"}


def _priced(card: dict, status: str, estimate: Optional[dict] = None, **kw) -> dict:
    return {**card, "price_status": status, "estimated_price": estimate, **kw}


class TestPriceStatusDisplay:
    def search(self, products: list[dict]) -> dict:
        resp = copy.deepcopy(SEARCH)
        resp["products"], resp["result_product_ids"] = products, [p["id"] for p in products]
        return resp

    def test_estimated_card_is_labelled_synthetic(self) -> None:
        est_card = _priced(CARD3, "estimated", EST, price=None, currency=None)
        text = _text(_say(_app(FakeClient([self.search([est_card, CARD1])])), "sneakers under ₹4000"))
        assert "≈ ₹2,340" in text and "Estimated price · Not a retailer price" in text
        assert "₹1,500–₹6,000 range" in text and "The source has no price" in text
        assert "₹6,646" in text and "Converted from source price" in text  # the other card keeps its INR price

    def test_verified_and_unknown_cards_have_no_estimate_label(self) -> None:
        unknown = _priced(CARD1, "unknown", price=None, currency=None)
        source = _priced(CARD3, "verified", price_conversion=None)  # an INR source price
        text = _text(_say(_app(FakeClient([self.search([source, unknown])])), "bags"))
        assert "₹4,153" in text and "Source price" in text and "Price unavailable" in text
        assert "estimate" not in text.lower() and "Converted from source price" not in text

    def test_compare_table_marks_estimates(self) -> None:
        cmp = self.search([_priced(CARD3, "estimated", EST, price=None, currency=None), _priced(CARD1, "verified")])
        cmp["intent"] = "COMPARE"
        at = _say(_app(FakeClient([cmp])), "compare")
        table = str(at.table[0].value.to_dict())
        assert "≈ ₹2,340 (estimate, not a retailer price)" in table and "₹6,646 (source price)" in table
        assert "$" not in table

    def test_old_responses_never_show_a_dollar_price(self) -> None:
        old = {k: v for k, v in CARD3.items() if k not in ("price_status", "price_conversion")}
        old.update(price=49.99, currency="USD")  # a response from before every price was INR
        text = _text(_say(_app(FakeClient([self.search([old])])), "backpack"))
        assert "Price unavailable" in text and "49.99" not in text and "&#36;" not in text


PROFILE_KEYS = ("new-profile-name", "create-profile", "open-profile-id", "open-profile", "consent-toggle",
                "pref-add", "pref-add-key", "pref-add-value", "export", "export-dl", "delete-confirm",
                "delete-profile", "forget-profile")


class TestNoProfiles:
    """User profiles were removed (2026-10-21): the app opens straight to product search."""

    def test_opens_directly_to_search_with_no_profile_controls(self) -> None:
        at = _app(FakeClient())
        assert at.text_input(key="hero-query") is not None
        keys = {w.key for kind in ("button", "text_input", "number_input", "toggle", "selectbox")
                for w in getattr(at, kind)}
        assert not keys & set(PROFILE_KEYS)
        assert "profile" not in _text(at).lower()

    def test_cards_have_no_save_or_feedback_buttons(self) -> None:
        at = _say(_app(FakeClient()), "backpack")
        assert not [b for b in at.button if b.key and b.key.startswith(("save-", "up-", "down-", "sugg-"))]
        labels = {b.label for b in at.button}
        assert not {"Save", "Helpful", "Not for me"} & labels
        assert "Ask about this" in labels

    def test_chat_never_sends_a_user_id(self) -> None:
        client = FakeClient()
        _say(_app(client), "backpack")
        assert client.named("chat") == [("chat", "backpack", None, None)]


REAL_RUNNING_SHOES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "chat_response_find_running_shoes.json").read_text(encoding="utf-8"))


class TestRealResponseRegression:
    """2026-10-22: "Find running shoes" crashed the UI at ``render_turn(i, msg["response"])``.

    Cause: a Streamlit process started before the profile removal kept the old
    ``render_turn(index, response, latest)`` in memory while re-reading the new
    app.py. These tests pin the call/signature agreement and render the exact
    response the backend returned for that query.
    """

    def test_every_render_turn_call_in_app_matches_its_signature(self) -> None:
        import ast
        import inspect

        from frontend.components.turn import render_turn

        sig = inspect.signature(render_turn)
        calls = [n for n in ast.walk(ast.parse(Path(APP).read_text(encoding="utf-8")))
                 if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "render_turn"]
        assert calls
        for call in calls:
            sig.bind(*range(len(call.args)), **{k.arg: None for k in call.keywords})  # raises TypeError

    def test_real_backend_response_renders(self) -> None:
        at = _say(_app(FakeClient([REAL_RUNNING_SHOES])), "Find running shoes")
        text = _text(at)
        assert "Marathon Running Shoes - Men" in text
        assert REAL_RUNNING_SHOES["answer"]["summary"][:40] in text
        assert "profile" not in text.lower()

    def test_history_with_several_real_turns_renders(self) -> None:
        # The crash was in the history loop: every earlier turn is re-rendered on each run.
        at = _app(FakeClient([REAL_RUNNING_SHOES]))
        for _ in range(3):
            at = _say(at, "Find running shoes")
        at.toggle(key="show-earlier").set_value(True).run()
        assert not at.exception
        assert _text(at).count("Marathon Running Shoes - Men") >= 3


class TestSimplifiedLayout:
    """2026-10-22 simplification: no sidebar, one search box, compact filter row."""

    def test_no_sidebar_and_no_second_search_input(self) -> None:
        at = _say(_app(FakeClient()), "backpack")
        assert len(at.sidebar.children) == 0
        assert len(at.chat_input) == 0  # the search box is the only input
        assert [t.key for t in at.text_input] == ["hero-query"]
        text = _text(at)
        for gone in ("Status", "Active filters", "Remote product images", "API: connected"):
            assert gone not in text

    def test_filter_row_hidden_without_filters_and_shown_with_them(self) -> None:
        at = _say(_app(FakeClient()), "backpack")  # SEARCH has no constraint report
        assert '<span class="ss-filter">' not in _text(at) and not [b for b in at.button if (b.key or "").startswith("chip-")]
        r = _with_constraints(SEARCH, [
            {"kind": "product_type", "label": "Product type: footwear", "status": "applied", "value": ["footwear"]},
            {"kind": "min_rating", "label": "Catalogue rating ≥ 4.0", "status": "applied", "value": 4.0}])
        at = _say(_app(FakeClient([r])), "shoes 4+ stars")
        assert '<span class="ss-filter">Footwear</span>' in _text(at)
        assert at.button(key="chip-min_rating").label == "4+ stars ✕"

    def test_latest_answer_first_and_earlier_results_on_request(self) -> None:
        first, second = copy.deepcopy(SEARCH), copy.deepcopy(SEARCH)
        second["products"] = [CARD1]
        second["result_product_ids"] = [1]
        at = _say(_say(_app(FakeClient([first, second])), "backpack"), "earbuds")
        queries = [m.value for m in at.markdown if 'class="ss-query"' in str(m.value)]
        assert queries == ['<div class="ss-results-head"><span class="ss-query">earbuds</span>'
                           '<span class="ss-count">1 exact match</span></div>']
        assert "Hiking Backpack" not in _text(at)
        at.toggle(key="show-earlier").set_value(True).run()
        assert "Hiking Backpack" in _text(at)

    def test_examples_and_compare_show_one_loading_path(self) -> None:
        client = FakeClient()
        at = _app(client)
        at.button(key="example-1").click().run()
        assert client.named("chat")[-1][1] == "red silk saree under ₹3,000"
        assert at.session_state["pending"] is None

    def test_card_title_is_an_escaped_two_line_heading(self) -> None:
        r = copy.deepcopy(SEARCH)
        r["products"][0]["title"] = 'Pack <b>"XL"</b> $5 & more'
        text = _text(_say(_app(FakeClient([r])), "backpack"))
        assert ('<div class="ss-title" role="heading" aria-level="4" title="Pack &lt;b&gt;&quot;XL&quot;&lt;/b&gt; '
                '[amount removed: prices are shown in ₹] &amp; more">Pack &lt;b&gt;&quot;XL&quot;&lt;/b&gt; [amount removed: prices are shown in ₹] &amp; more</div>') in text


ZERO_RED_DRESS = {
    **{k: v for k, v in SEARCH.items()},
    "answer": {"summary": "No matching products were found in the catalog for your query.", "recommendations": [],
               "insufficient_evidence": True, "missing_information": "No products matching"},
    "support_report": {**SEARCH["support_report"], "is_valid": True, "claim_results": []},
    "products": [], "result_product_ids": [], "evidence": [], "generation": {"mode": "none", "reason": "no_products_retrieved"},
    "constraints": {"items": [
        {"kind": "product_type", "label": "Product type: dress", "status": "applied", "value": ["dress"]},
        {"kind": "colour", "label": "Colour: red (stated by the listing)", "status": "applied", "value": "red"},
        {"kind": "price", "label": "Max price: INR 2,000.00", "status": "applied",
         "value": {"min": None, "max": 2000.0, "currency": "INR"}}],
        "matching_products": 0, "catalog_currencies": ["USD"], "unknown_attribute_products": 12},
}


class TestEditorialLayout:
    """2026-10-22: header with New search, exact-match counts, broaden options, colour chips."""

    def test_new_search_resets_the_conversation(self) -> None:
        at = _say(_app(FakeClient()), "backpack")
        at.button(key="new-search").click().run()
        assert at.session_state["messages"] == [] and at.session_state["session_id"] is None
        assert "Find exactly what you have in mind." in _text(at)
        assert not [b for b in at.button if b.key == "new-search"]  # only shown once there is something to reset

    def test_zero_matches_offer_separate_broader_searches(self) -> None:
        client = FakeClient([ZERO_RED_DRESS, SEARCH])
        at = _say(_app(client), "red summer dress under ₹2000")
        text = _text(at)
        assert "No exact matches" in text
        assert '<span class="ss-filter">Red</span>' in text and "Under ₹2,000 ✕" in [b.label for b in at.button]
        assert "12 other products match everything else but don't state their colour" in text
        broaden = {b.label: b.key for b in at.button if (b.key or "").startswith("broaden-")}
        assert set(broaden) == {"Any colour (not just red)", "Any price"}
        at.button(key=broaden["Any colour (not just red)"]).click().run()
        assert client.named("chat")[-1][1] == "summer dress under ₹2000"  # a new search, not mixed in

    def test_results_show_an_exact_match_count_and_no_broaden_buttons(self) -> None:
        at = _say(_app(FakeClient()), "backpack")
        assert '<span class="ss-count">2 exact matches</span>' in _text(at)
        assert not [b for b in at.button if (b.key or "").startswith("broaden-")]

    def test_health_is_not_requeried_on_unrelated_interactions(self) -> None:
        class CountingClient(FakeClient):
            health_calls = 0

            def health(self):
                CountingClient.health_calls += 1
                return super().health()

        client = CountingClient()
        at = _say(_app(client), "backpack")
        before = CountingClient.health_calls
        at.checkbox(key="cmp-1-3").check().run()
        at.button(key="ask-1-3").click().run()
        assert CountingClient.health_calls == before


# ---------------------------------------------------------------------------
# Product-first search and the optional AI summary (2026-10-22)
# ---------------------------------------------------------------------------

PRODUCT_FIRST = {
    **SEARCH,
    "answer": {"summary": "", "recommendations": [], "insufficient_evidence": False, "missing_information": None},
    "support_report": {**SEARCH["support_report"], "total_claims": 0, "supported_claims": 0, "unsupported_claims": 0,
                       "support_rate": 1.0, "is_valid": True, "fabricated_citations": 0, "claim_results": []},
    "result_product_ids": [1, 3],
    "products": [{**CARD1, "recommended": False}, {**CARD3, "recommended": False}],
    "evidence": [],
    "generation": {"mode": "none", "reason": "summary_not_requested", "model": None, "attempts": None},
}
# The summary of the same results: same products, same order, now with the validated answer.
SUMMARISED = {**SEARCH, "result_product_ids": [1, 3], "products": [CARD1, CARD3]}


class SummaryClient(FakeClient):
    def __init__(self, summary_responses: Optional[list[Any]] = None, **kw: Any) -> None:
        super().__init__([PRODUCT_FIRST], **kw)
        self.summary_responses = list(summary_responses or [SUMMARISED])

    def summary(self, session_id, result_product_ids=None):
        self._rec("summary", session_id, result_product_ids)
        nxt = self.summary_responses.pop(0) if len(self.summary_responses) > 1 else self.summary_responses[0]
        if isinstance(nxt, Exception):
            raise nxt
        return copy.deepcopy(nxt)


class _NowExecutor:
    """Runs the job at once (a finished Future), so a test sees the outcome on the next run."""

    def submit(self, fn, *args):
        from concurrent.futures import Future

        f: Future = Future()
        try:
            f.set_result(fn(*args))
        except BaseException as e:  # noqa: BLE001 - delivered through the Future, as a pool would
            f.set_exception(e)
        return f


class _NeverExecutor:
    """A job that is still running."""

    def __init__(self) -> None:
        self.submitted = 0

    def submit(self, fn, *args):
        from concurrent.futures import Future

        self.submitted += 1
        return Future()


def _card_titles(at: AppTest) -> list[str]:
    return [m.value for m in at.markdown if 'class="ss-title"' in str(m.value)]


class TestOptionalSummary:
    def test_search_shows_products_without_a_summary_or_model_call(self, monkeypatch) -> None:
        monkeypatch.setattr("frontend.state.SUMMARY_EXECUTOR", _NowExecutor())
        client = SummaryClient()
        at = _say(_app(client), "backpack")
        text = _text(at)
        assert "Hiking Backpack" in text and "Wireless Earbuds" in text
        assert 'class="ss-summary"' not in text and "Top pick" not in text   # no AI text until asked
        assert '<span class="ss-count">2 exact matches</span>' in text
        assert [b.label for b in at.button if b.key == "summarise-1"] == ["Summarise reviews"]
        assert client.named("summary") == []

    def test_summary_runs_only_when_asked_and_keeps_the_card_order(self, monkeypatch) -> None:
        monkeypatch.setattr("frontend.state.SUMMARY_EXECUTOR", _NowExecutor())
        client = SummaryClient()
        at = _say(_app(client), "backpack")
        order = _card_titles(at)
        at.button(key="summarise-1").click().run()
        assert not at.exception, [e.value for e in at.exception]
        at.run()  # the page redraws with the finished summary
        assert client.named("summary") == [("summary", "s1", [1, 3])]
        text = _text(at)
        assert "The backpack costs [amount removed: prices are shown in ₹] and is great" in text           # the AI summary
        assert "Very light and waterproof" in text and "Top pick" in text
        assert _card_titles(at) == order                              # same cards, same order
        assert not [b for b in at.button if b.key == "summarise-1"]  # done: no second request offered
        at.run()
        assert len(client.named("summary")) == 1

    def test_results_stay_usable_while_the_summary_runs(self, monkeypatch) -> None:
        never = _NeverExecutor()
        monkeypatch.setattr("frontend.state.SUMMARY_EXECUTOR", never)
        at = _say(_app(SummaryClient()), "backpack")
        at.button(key="summarise-1").click().run()
        assert "Summarising reviews of the top matches" in _text(at)
        assert "Hiking Backpack" in _text(at)                       # results preserved
        assert not [b for b in at.button if b.key == "summarise-1"]  # no duplicate request possible
        at.checkbox(key="cmp-1-3").check().run()                    # the page still responds
        at.button(key="ask-1-3").click().run()
        assert not at.exception
        assert "Summarising reviews of the top matches" in _text(at)
        assert never.submitted == 1                                 # reruns did not resubmit

    def test_repeated_requests_while_running_start_one_job(self, monkeypatch) -> None:
        """Double clicks or stale widgets reaching the callback twice start one job only."""
        from types import SimpleNamespace

        from frontend import state

        never = _NeverExecutor()
        client = SummaryClient()
        fake_st = SimpleNamespace(session_state={
            "messages": [{"role": "user", "text": "backpack", "response": None},
                         {"role": "assistant", "text": "", "response": copy.deepcopy(PRODUCT_FIRST)}],
            "session_id": "s1", "summaries": {}, "client": client,
        })
        monkeypatch.setattr(state, "st", fake_st)
        monkeypatch.setattr(state, "SUMMARY_EXECUTOR", never)
        state.request_summary(1)
        state.request_summary(1)
        assert never.submitted == 1
        assert state.poll_summary(1) == "running"

    def test_failed_summary_keeps_results_and_offers_retry(self, monkeypatch) -> None:
        monkeypatch.setattr("frontend.state.SUMMARY_EXECUTOR", _NowExecutor())
        client = SummaryClient([ApiError("timeout", "No response within 120 s"), SUMMARISED])
        at = _say(_app(client), "backpack")
        at.button(key="summarise-1").click().run()
        at.run()
        text = _text(at)
        assert "The summary could not be generated." in text
        assert "Hiking Backpack" in text and "Wireless Earbuds" in text   # results untouched
        at.button(key="summarise-retry-1").click().run()
        at.run()
        assert "The backpack costs [amount removed: prices are shown in ₹] and is great" in _text(at)
        assert len(client.named("summary")) == 2

    def test_summary_of_replaced_results_is_discarded(self, monkeypatch) -> None:
        monkeypatch.setattr("frontend.state.SUMMARY_EXECUTOR", _NowExecutor())
        stale = {**SUMMARISED, "result_product_ids": [9, 8]}
        at = _say(_app(SummaryClient([stale])), "backpack")
        at.button(key="summarise-1").click().run()
        at.run()
        text = _text(at)
        assert "The backpack costs [amount removed: prices are shown in ₹] and is great" not in text
        assert at.session_state["messages"][1]["response"]["result_product_ids"] == [1, 3]

    def test_new_search_drops_a_running_summary(self, monkeypatch) -> None:
        monkeypatch.setattr("frontend.state.SUMMARY_EXECUTOR", _NeverExecutor())
        at = _say(_app(SummaryClient()), "backpack")
        at.button(key="summarise-1").click().run()
        at.button(key="new-search").click().run()
        assert at.session_state["summaries"] == {}

    def test_searching_shows_search_progress_not_ai_wording(self) -> None:
        src = Path(APP).read_text(encoding="utf-8")
        assert "Finding matching products" in src
        assert "one to two minutes" not in src


class TestBranding:
    """2026-10-22: the application is called "Self Search" wherever a user can see it."""

    def test_header_and_page_use_the_new_name(self) -> None:
        at = _say(_app(FakeClient()), "backpack")
        text = _text(at)
        assert '<div class="ss-brand">Self <span>Search</span></div>' in text
        assert "ShelfSense" not in text and "Shelf<span>" not in text
        assert "Academic research prototype" in text  # the disclaimer is kept

    def test_unreachable_message_and_greeting_use_the_new_name(self) -> None:
        from frontend.view_models import error_message
        from src.agent.intent_router import SMALLTALK_RESPONSES
        from src.api.app import app as api

        assert "Self Search is not reachable" in error_message("unreachable")
        assert "Self Search is running" in error_message("timeout")
        assert "I'm Self Search" in SMALLTALK_RESPONSES["greeting"]
        assert api.title == "Self Search API"
        down = _app(FakeClient(health="down"))
        assert "Self Search is not reachable" in _text(down)


class TestImageFallback:
    """2026-10-23: the main image, then another validated photo of the same product, then
    "Image unavailable"; never another product's image."""

    def test_fallback_image_is_used_when_the_main_one_fails(self) -> None:
        r = copy.deepcopy(SEARCH)
        r["products"][0]["image_url"] = "https://img.shop.test-cdn.org/broken.jpg"
        r["products"][0]["image_alternates"] = ["https://img.shop.test-cdn.org/second.jpg"]

        class Client(FakeClient):
            def fetch_image(self, url):
                self._rec("fetch_image", url)
                return None if url.endswith("broken.jpg") else (b"\x89PNG" * 600, "image/png")

        client = Client([r])
        text = _text(_say(_app(client, load_remote=True), "backpack"))
        fetched = [c[1] for c in client.named("fetch_image")]
        assert "https://img.shop.test-cdn.org/broken.jpg" in fetched and "https://img.shop.test-cdn.org/second.jpg" in fetched
        assert 'alt="Product image of Hiking Backpack"' in text

    def test_no_valid_image_shows_image_unavailable(self) -> None:
        r = copy.deepcopy(SEARCH)
        r["products"][0]["image_alternates"] = []
        client = FakeClient([r])
        client.image = None  # every fetch fails
        text = _text(_say(_app(client, load_remote=True), "backpack"))
        assert 'alt="Image unavailable for Hiking Backpack"' in text
