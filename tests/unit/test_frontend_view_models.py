"""Unit tests for the Phase 9 frontend's pure presentation rules and API client."""

from __future__ import annotations

from typing import Any, Optional

import pytest
import requests

from frontend.api_client import MAX_IMAGE_BYTES, MIN_IMAGE_BYTES, ApiError, ShelfSenseClient
from frontend.config import FrontendConfig
from frontend.view_models import (
    PLACEHOLDER_DATA_URI,
    annotate_recommendations,
    card_details,
    card_price_label,
    compare_rows,
    conversion_note,
    error_message,
    escape_md,
    filter_checks,
    filter_row,
    generation_notice,
    image_alt,
    image_plan,
    inr,
    img_html,
    insufficient_notice,
    local_reviews_label,
    money,
    price_label,
    source_price_note,
    rating_label,
    validation_banner,
)


def _response(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "answer": {
            "summary": "s",
            "recommendations": [{
                "product_id": 3,
                "why_it_matches": [
                    {"claim": "Very light", "evidence_ids": ["R1"]},
                    {"claim": "[UNVERIFIED] Costs $1", "evidence_ids": ["R99"]},
                    {"claim": "Never validated", "evidence_ids": ["R1"]},
                ],
                "watch_outs": [{"claim": "[UNVERIFIED] Very light", "evidence_ids": ["R2"]}],
            }],
            "insufficient_evidence": False,
        },
        "support_report": {
            "total_claims": 3, "supported_claims": 1, "support_rate": 1 / 3, "is_valid": False,
            "fabricated_citations": 1, "numeric_mismatches": 0, "cross_product_mismatches": 0,
            "claim_results": [
                {"claim_text": "Very light", "product_id": 3, "status": "SUPPORTED"},
                {"claim_text": "Costs $1", "product_id": 3, "status": "FABRICATED_CITATION"},
                {"claim_text": "Very light", "product_id": 3, "status": "UNSUPPORTED_LOW_SIMILARITY"},
            ],
        },
        "evidence": [{"evidence_id": "R1", "product_id": 3}, {"evidence_id": "R2", "product_id": 3}],
    }
    base.update(over)
    return base


class TestClaims:
    def test_status_mapping_and_prefix_stripping(self) -> None:
        rec = annotate_recommendations(_response())[3]
        why = {c.text: c for c in rec.why}
        assert why["Very light"].supported and why["Very light"].label == "Supported by cited reviews"
        assert why["Costs $1"].status == "FABRICATED_CITATION" and not why["Costs $1"].supported
        assert why["Costs $1"].missing_evidence_ids == ["R99"]
        # A claim with no validation result is never shown as verified
        assert (why["Never validated"].status, why["Never validated"].label) == (None, "Unverified")
        # Duplicate claim text consumes results in order: the watch-out gets the second result
        assert rec.watch_outs[0].status == "UNSUPPORTED_LOW_SIMILARITY"

    def test_every_status_has_words_not_just_an_icon(self) -> None:
        for c in annotate_recommendations(_response())[3].why:
            assert len(c.label.split()) >= 1 and c.label[0].isalpha()

    def test_validation_banner(self) -> None:
        banner = validation_banner(_response())
        assert banner.startswith("Only 1 of 3 claims are backed by the cited reviews")
        assert "1 cite a missing review" in banner and "marked ⚠️" in banner
        valid = _response(support_report={"is_valid": True, "claim_results": []})
        assert validation_banner(valid) is None
        assert "unverified" in validation_banner(_response(support_report=None))
        assert validation_banner(_response(answer=None)) is None

    def test_insufficient_and_generation_notices(self) -> None:
        r = _response(answer={"summary": "", "insufficient_evidence": True, "missing_information": "No products"})
        assert insufficient_notice(r).endswith("Missing: No products")
        assert insufficient_notice(_response()) is None
        assert "AI model is not running" in generation_notice(
            {"generation": {"mode": "fallback", "reason": "provider_unavailable"}})
        assert generation_notice({"generation": {"mode": "llm"}}) is None
        assert generation_notice({"generation": None}) is None


class TestCards:
    def test_labels(self) -> None:
        assert price_label(4153.0, "INR") == "₹4,153" and price_label(None, "INR") == "Price unavailable"
        assert price_label(49.99, "USD") == "Price unavailable"  # a non-INR amount is never displayed
        assert rating_label(4.25, 1250) == "4.2★ · 1,250 ratings" and rating_label(4.0, 1) == "4.0★ · 1 rating"
        assert rating_label(None, None) == "No rating yet"
        assert local_reviews_label(1) == "1 review" and local_reviews_label(0) == "No reviews"

    def test_money_is_inr_only_with_indian_grouping(self) -> None:
        assert money(2150.0, "INR") == "₹2,150" and money(2150.5, "INR") == "₹2,150.50"
        assert [inr(x) for x in (799, 1499, 12999, 125000, 10000000)] == [
            "₹799", "₹1,499", "₹12,999", "₹1,25,000", "₹1,00,00,000"]
        # Never converts and never swaps a symbol: any other currency is not displayed.
        for code in ("USD", "EUR", None):
            assert money(22.99, code) == "Price unavailable"

    def test_estimated_price_is_never_a_plain_price(self) -> None:
        est = {"price_status": "estimated", "price": None, "currency": None,
               "estimated_price": {"amount": 2340.0, "currency": "INR", "range_min": 1500.0, "range_max": 6000.0,
                                   "config_version": "illustrative-inr-v1"}}
        assert card_price_label(est) == "≈ ₹2,340"  # the card shows the ESTIMATE_NOTE chip beside it
        assert card_price_label(est, with_note=True) == "≈ ₹2,340 (estimate, not a retailer price)"
        assert card_price_label({"price_status": "verified", "price": 4999.0, "currency": "INR"}) == "₹4,999"

    def test_every_displayed_price_is_inr(self) -> None:
        """2026-10-23: INR source, converted, estimated and unavailable prices; never a dollar amount."""
        source = {"price_status": "verified", "price": 1499.0, "currency": "INR", "estimated_price": None}
        converted = {"price_status": "converted", "price": 1968.0, "currency": "INR", "estimated_price": None,
                     "price_conversion": {"from_currency": "USD", "rate": 83.0815, "rate_date": "2023-09-29",
                                          "source": "European Central Bank euro foreign exchange reference rates"}}
        est = {"price_status": "estimated", "price": None, "currency": None,
               "estimated_price": {"amount": 1480.0, "currency": "INR", "range_min": 800.0, "range_max": 4000.0,
                                   "config_version": "illustrative-inr-v1"}}
        unknown = {"price_status": "unknown", "price": None, "currency": None, "estimated_price": None}

        assert card_price_label(source) == "₹1,499" and source_price_note(source) == "Source price"
        assert card_price_label(source, with_note=True) == "₹1,499 (source price)"
        assert card_price_label(converted) == "₹1,968" and source_price_note(converted) == "Converted from source price"
        assert card_price_label(converted, with_note=True) == "₹1,968 (converted from the source price)"
        assert conversion_note(converted) == ("The source lists this price in another currency; it is converted "
                                              "to rupees at ₹83.08 per US dollar (ECB reference rate, 2023-09-29).")
        assert card_price_label(est) == "≈ ₹1,480" and source_price_note(est) is None and conversion_note(est) is None
        assert card_price_label(unknown) == "Price unavailable" and source_price_note(unknown) is None

        for card in (source, converted, est, unknown):
            text = " ".join(filter(None, [card_price_label(card), card_price_label(card, with_note=True),
                                          source_price_note(card), conversion_note(card)]))
            assert "$" not in text and "USD" not in text

        # An old-style response with a USD amount is never shown as dollars or as rupees.
        old = {"price_status": "verified", "price": 23.69, "currency": "USD", "estimated_price": None}
        assert card_price_label(old) == "Price unavailable" and source_price_note(old) is None
        # The symbol comes from the API's currency, never from the size of the number.
        assert card_price_label({**est, "estimated_price": {**est["estimated_price"], "amount": 9.0}}) == "≈ ₹9"
        no_currency = {**est, "estimated_price": {k: v for k, v in est["estimated_price"].items() if k != "currency"}}
        assert "₹" not in card_price_label(no_currency)

    def test_card_details_are_brand_then_short_features(self) -> None:
        card = {"brand": "Acme", "features": ["Breathable mesh upper that keeps feet cool all day long", "x"]}
        assert card_details(card) == ["Acme", "Breathable mesh upper that keeps feet c…"]
        assert card_details({"features": []}) == []

    @pytest.mark.parametrize("url,load,expected", [
        (None, True, ("placeholder", "missing")),
        ("https://shop.test-cdn.org/a.jpg", False, ("placeholder", "remote_images_disabled")),
        ("https://example.com/img/a.jpg", True, ("placeholder", "reserved_example_domain")),
        ("https://img.example.org/a.jpg", True, ("placeholder", "reserved_example_domain")),
        ("https://cdn.shop.invalid/a.jpg", True, ("placeholder", "reserved_example_domain")),
        ("ftp://cdn.shop.com/a.jpg", True, ("placeholder", "invalid_url")),
        ("https://cdn.shop.com/a.jpg", True, ("remote", "https://cdn.shop.com/a.jpg")),
    ])
    def test_image_plan(self, url, load, expected) -> None:
        assert image_plan(url, load) == expected

    def test_img_html_escapes_alt_and_src(self) -> None:
        tag = img_html(PLACEHOLDER_DATA_URI, image_alt({"title": 'Bag "XL" <b>'}, placeholder=True))
        assert 'alt="Image unavailable for Bag &quot;XL&quot; &lt;b&gt;"' in tag
        assert "<b>" not in tag

    def test_filter_checks_and_chips(self) -> None:
        card = {"price": 49.99, "currency": "USD", "catalog_rating": 4.3}
        # The fallback never sets a source price against a budget whose currency it doesn't know
        # (a USD price under a rupee budget); the API's constraint checks cover prices.
        assert filter_checks(card, {"max_price": 3000.0, "min_rating": 4.0}) == [
            "Catalogue rating 4.3 meets your minimum of 4.0"]
        assert filter_checks({"price": None}, {"max_price": 60.0}) == []

    def test_compare_rows_mark_missing_aspects(self) -> None:
        rows = compare_rows([{"id": 1, "title": "A", "price": 83.0, "currency": "INR", "price_status": "converted",
                              "aspects": []},
                             {"id": 2, "title": "B", "price": None, "aspects": []}])
        by_attr = {r["Attribute"]: r for r in rows}
        assert by_attr["Price"] == {"Attribute": "Price", "A": "₹83 (converted from the source price)",
                                    "B": "Price unavailable"}
        assert {"Price", "Rating", "Reviews", "Brand", "Features"} == set(by_attr)

    def test_escape_md(self) -> None:
        assert escape_md("costs $5 to $10 [link](http://x) *bold* <b>") == \
            r"costs \$5 to \$10 \[link\](http://x) \*bold\* \<b\>"
        assert escape_md("USD 49.99 - great") == "USD 49.99 - great"  # inline text untouched
        assert escape_md("- item\n1. first\n# head") == "\\- item\n\\1. first\n\\# head"


def test_error_messages() -> None:
    assert "not reachable" in error_message("unreachable")
    assert error_message("validation", "bad ids").endswith("bad ids")
    assert "temporarily unavailable" in error_message("unavailable")
    assert "profile" not in " ".join(error_message(k) for k in ("conflict", "unavailable")).lower()


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status: int, body: Any = None, headers: Optional[dict] = None, raw: bytes = b"") -> None:
        import json as _json
        import io

        self.status_code = status
        self._body = body
        self.content = b"" if body is None else _json.dumps(body).encode()
        self.text = self.content.decode()
        self.headers = headers or {}
        self.raw = io.BytesIO(raw)
        self.raw.read = (lambda n=-1, decode_content=True, _r=self.raw: io.BytesIO.read(_r, n))  # type: ignore

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _Session:
    def __init__(self, resp: Any = None, exc: Optional[Exception] = None) -> None:
        self.resp, self.exc, self.calls = resp, exc, []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append((method, url, json, headers))
        if self.exc:
            raise self.exc
        return self.resp

    def get(self, url, timeout=None, stream=False):
        self.calls.append(("GET", url, None, None))
        if self.exc:
            raise self.exc
        return self.resp


class TestApiClient:
    def test_key_sent_as_header_and_body_shape(self) -> None:
        s = _Session(_Resp(200, {"ok": True}))
        c = ShelfSenseClient(FrontendConfig(api_url="http://api:8000", api_key="k1"), session=s)
        c.chat("hi", session_id="s1", product_ids=[3, 1])
        method, url, body, headers = s.calls[0]
        assert (method, url) == ("POST", "http://api:8000/chat")
        assert body == {"message": "hi", "session_id": "s1", "product_ids": [3, 1]}
        assert headers == {"X-API-Key": "k1"}

    def test_no_key_no_header_and_minimal_body(self) -> None:
        s = _Session(_Resp(200, {}))
        ShelfSenseClient(FrontendConfig(), session=s).chat("hi")
        assert s.calls[0][2] == {"message": "hi"} and s.calls[0][3] == {}

    @pytest.mark.parametrize("status,kind", [
        (401, "auth"), (404, "not_found"), (409, "conflict"), (422, "validation"),
        (503, "unavailable"), (500, "server"), (418, "other"),
    ])
    def test_error_kinds(self, status, kind) -> None:
        c = ShelfSenseClient(FrontendConfig(), session=_Session(_Resp(status, {"detail": "nope"})))
        with pytest.raises(ApiError) as e:
            c.product(1)
        assert (e.value.kind, e.value.status, e.value.detail) == (kind, status, "nope")

    def test_pydantic_validation_detail_list_is_flattened(self) -> None:
        body = {"detail": [{"msg": "field required"}, {"msg": "too long"}]}
        c = ShelfSenseClient(FrontendConfig(), session=_Session(_Resp(422, body)))
        with pytest.raises(ApiError) as e:
            c.chat("x")
        assert e.value.detail == "field required; too long"

    def test_unreachable(self) -> None:
        c = ShelfSenseClient(FrontendConfig(), session=_Session(exc=requests.ConnectionError("refused")))
        with pytest.raises(ApiError) as e:
            c.health()
        assert e.value.kind == "unreachable"

    def test_fetch_image_rules(self) -> None:
        png = b"\x89PNG" + b"0" * MIN_IMAGE_BYTES
        ok = _Resp(200, headers={"Content-Type": "image/png"}, raw=png)
        c = ShelfSenseClient(FrontendConfig(), session=_Session(ok))
        assert c.fetch_image("https://cdn.shop.com/a.png") == (png, "image/png")
        pixel = _Resp(200, headers={"Content-Type": "image/gif"}, raw=b"GIF89a" + b"0" * 30)  # blank placeholder
        assert ShelfSenseClient(FrontendConfig(), session=_Session(pixel)).fetch_image("https://x.com/a") is None
        assert c.fetch_image("file:///etc/passwd") is None
        html_page = _Resp(200, headers={"Content-Type": "text/html"}, raw=b"<html>")
        assert ShelfSenseClient(FrontendConfig(), session=_Session(html_page)).fetch_image("https://x.com/a") is None
        big = _Resp(200, headers={"Content-Type": "image/jpeg"}, raw=b"0" * (MAX_IMAGE_BYTES + 5))
        assert ShelfSenseClient(FrontendConfig(), session=_Session(big)).fetch_image("https://x.com/a") is None
        down = ShelfSenseClient(FrontendConfig(), session=_Session(exc=requests.ConnectionError()))
        assert down.fetch_image("https://x.com/a") is None


def test_config_from_env() -> None:
    cfg = FrontendConfig.from_env({"SHELFSENSE_API_URL": "http://api:8000/", "API_KEY": "k",
                                   "SHELFSENSE_LOAD_REMOTE_IMAGES": "True"})
    assert (cfg.api_url, cfg.api_key, cfg.load_remote_images) == ("http://api:8000", "k", True)
    default = FrontendConfig.from_env({})
    assert (default.api_url, default.api_key, default.load_remote_images) == ("http://127.0.0.1:8000", None, False)


# ---------------------------------------------------------------------------
# Relevance fix and redesign (Phase 9)
# ---------------------------------------------------------------------------

def _constraints(*items, matching=1, currencies=("USD",)):
    return {"items": list(items), "matching_products": matching, "catalog_currencies": list(currencies)}


class TestConstraintView:
    def test_applied_and_not_applied_items(self) -> None:
        from frontend.view_models import constraint_view

        r = {"constraints": _constraints(
            {"kind": "product_type", "label": "Product type: footwear", "status": "applied"},
            {"kind": "price", "label": "Max price: INR 4,000.00", "status": "not_applied",
             "reason": "Your budget is in INR, but catalogue prices are in USD."},
        )}
        view = constraint_view(r)
        assert view.applied == ["Product type: footwear"]
        assert view.not_applied == [("Max price: INR 4,000.00", "Your budget is in INR, but catalogue prices are in USD.")]
        assert view.matching_products == 1 and view.catalog_currencies == ["USD"]
        assert constraint_view({"constraints": None}) is None

class TestFilterRow:
    """Compact chips under the search bar, built from what the API actually enforced."""

    def test_short_labels_for_each_kind(self) -> None:
        r = {"constraints": _constraints(
            {"kind": "product_type", "label": "Product type: footwear (…)", "status": "applied", "value": ["footwear"]},
            {"kind": "price", "label": "Max price: INR 3,000.00 (…)", "status": "applied",
             "value": {"min": None, "max": 3000.0, "currency": "INR", "uses_synthetic_estimates": True}},
            {"kind": "min_rating", "label": "Catalogue rating ≥ 4.0", "status": "applied", "value": 4.0},
            {"kind": "brand", "label": "Brand: Acme", "status": "applied", "value": "Acme"},
            {"kind": "excluded_term", "label": "Excludes products mentioning “leather”", "status": "applied",
             "value": "leather"},
            {"kind": "excluded_brand", "label": "Not brand Zed", "status": "applied", "value": "Zed"},
        )}
        chips = [(c.label, c.clear) for c in filter_row(r)]
        assert chips == [("Footwear", []), ("Under ₹3,000", ["max_price"]), ("4+ stars", ["min_rating"]),
                         ("Acme", ["brand"]), ("Without leather", []), ("Not Zed", [])]

    def test_price_ranges_and_minimums(self) -> None:
        def price(lo, hi, cur):
            return {"constraints": _constraints({"kind": "price", "label": "x", "status": "applied",
                                                 "value": {"min": lo, "max": hi, "currency": cur}})}
        assert [(c.label, c.clear) for c in filter_row(price(500.0, 800.0, "INR"))] == [
            ("₹500–₹800", ["min_price", "max_price"])]
        assert filter_row(price(2000.0, None, "INR"))[0].label == "From ₹2,000"
        assert filter_row(price(None, 125000.0, None))[0].label == "Under ₹1,25,000"
        assert filter_row(price(None, 50.0, "USD")) == []  # a non-INR budget is never shown

    def test_hidden_when_nothing_is_applied(self) -> None:
        blocked = {"constraints": _constraints({"kind": "price", "label": "x", "status": "not_applied",
                                                "value": {"currency": "INR"}})}
        assert filter_row(blocked) == [] and filter_row({"constraints": None}) == [] and filter_row(None) == []


class TestThemeAccessibility:
    def test_every_text_colour_pair_meets_wcag_aa(self) -> None:
        from frontend.theme import PALETTE, TEXT_PAIRS, contrast_ratio

        failing = [(fg, bg, round(contrast_ratio(PALETTE[fg], PALETTE[bg]), 2)) for fg, bg in TEXT_PAIRS
                   if contrast_ratio(PALETTE[fg], PALETTE[bg]) < 4.5]
        assert failing == []

    def test_streamlit_theme_matches_palette(self) -> None:
        import tomllib
        from pathlib import Path

        from frontend.theme import PALETTE

        theme = tomllib.loads((Path(__file__).resolve().parents[2] / ".streamlit" / "config.toml")
                              .read_text(encoding="utf-8"))["theme"]
        assert theme["base"] == "light"
        assert (theme["primaryColor"], theme["backgroundColor"], theme["secondaryBackgroundColor"],
                theme["textColor"], theme["borderColor"], theme["linkColor"]) == (
            PALETTE["cobalt"], PALETTE["cream"], PALETTE["paper"], PALETTE["ink"],
            PALETTE["widget_border"], PALETTE["cobalt_deep"])
        assert "sidebar" not in theme  # the UI has no sidebar (removed 2026-10-22)
        # Alert colours follow the palette's jobs (caution, problem, info, success).
        assert (theme["yellowBackgroundColor"], theme["yellowTextColor"]) == (PALETTE["mustard_tint"], PALETTE["mustard_deep"])
        assert (theme["redBackgroundColor"], theme["redTextColor"]) == (PALETTE["coral_tint"], PALETTE["coral_deep"])
        assert (theme["blueBackgroundColor"], theme["blueTextColor"]) == (PALETTE["cobalt_tint"], PALETTE["cobalt_deep"])
        assert (theme["greenBackgroundColor"], theme["greenTextColor"]) == (PALETTE["mint_tint"], PALETTE["mint_deep"])

    def test_fonts_are_local_bundled_families_only(self) -> None:
        import tomllib
        from pathlib import Path

        from frontend.theme import CSS

        theme = tomllib.loads((Path(__file__).resolve().parents[2] / ".streamlit" / "config.toml")
                              .read_text(encoding="utf-8"))["theme"]
        assert (theme["font"], theme["headingFont"], theme["codeFont"]) == ("sans-serif", "serif", "monospace")
        assert "fontFaces" not in theme
        for family in ('"Source Serif"', '"Source Sans"', '"Source Code Pro"'):
            assert family in CSS
        lowered = CSS.lower()
        assert "@import" not in lowered and "googleapis" not in lowered and "@font-face" not in lowered
        assert "url(\"http" not in lowered and "url(http" not in lowered

    def test_css_does_not_remove_focus_outlines(self) -> None:
        from frontend.theme import CSS

        assert "outline: none" not in CSS and "outline:none" not in CSS
        assert ":focus-visible" in CSS and "outline: 3px solid var(--ss-cobalt-deep)" in CSS

    def test_captions_are_not_faded(self) -> None:
        # Streamlit renders captions at opacity .6, which drops muted ink below 4.5:1 on cream.
        from frontend.theme import CSS

        assert '.stCaption p {{ color: var(--ss-ink-muted); opacity: 1; }}'.replace("{{", "{").replace("}}", "}") in CSS

    def test_chat_avatars_use_accessible_discs(self) -> None:
        from frontend.theme import CSS, PALETTE, contrast_ratio

        assert '[data-testid="stChatMessageAvatarUser"] { background-color: var(--ss-coral-deep)' in CSS
        assert '[data-testid="stChatMessageAvatarAssistant"] { background-color: var(--ss-cobalt)' in CSS
        assert contrast_ratio(PALETTE["white"], PALETTE["coral_deep"]) >= 4.5
        assert contrast_ratio(PALETTE["white"], PALETTE["cobalt"]) >= 4.5

    def test_non_text_ui_meets_3_to_1(self) -> None:
        from frontend.theme import PALETTE, UI_PAIRS, contrast_ratio

        failing = [(fg, bg) for fg, bg in UI_PAIRS if contrast_ratio(PALETTE[fg], PALETTE[bg]) < 3.0]
        assert failing == []

    def test_no_decorative_layer_or_ambient_animation(self) -> None:
        # 2026-10-22 redesign: the products are the visuals; no background shapes or drifting animation.
        from frontend.theme import CSS

        assert '[data-testid="stApp"]::before' not in CSS and '[data-testid="stApp"]::after' not in CSS
        assert "ss-drift" not in CSS and "data:image/svg+xml" not in CSS

    def test_reduced_motion_disables_animation(self) -> None:
        from frontend.theme import CSS

        block = CSS[CSS.index("@media (prefers-reduced-motion: reduce)"):]
        assert ".ss-skel i { animation: none !important; }" in block  # the only animation (loading)
        assert "transition: none !important" in block and "transform: none !important" in block

    def test_no_glass_or_glossy_effects(self) -> None:
        from frontend.theme import CSS

        lowered = CSS.lower()
        assert "backdrop-filter" not in lowered and "linear-gradient" not in lowered and "radial-gradient" not in lowered

    def test_category_accents_are_stable(self) -> None:
        from frontend.theme import category_accent

        assert category_accent("Electronics") == "cobalt"
        assert category_accent("Sports_and_Outdoors") == "mint"
        assert category_accent("Kitchen") == category_accent("kitchen") in ("lavender", "mustard", "coral", "cobalt", "mint")
        assert category_accent(None) == "cobalt"

    def test_claim_tone_never_marks_unverified_as_ok(self) -> None:
        from frontend.view_models import claim_tone

        assert claim_tone("SUPPORTED") == "ok"
        assert claim_tone("FABRICATED_CITATION") == claim_tone("NUMERIC_MISMATCH") == "bad"
        assert claim_tone("UNVERIFIABLE") == claim_tone(None) == "warn"


def test_feature_chip_text_shortens_long_source_features() -> None:
    from frontend.view_models import FEATURE_CHIP_CHARS, feature_chip_text

    long = "★High quality material,comfortable and soft,Reduces stress on joints strengthens and tones muscles"
    out = feature_chip_text(long)
    assert len(out) <= FEATURE_CHIP_CHARS and out.endswith("…") and long.startswith(out[:-1])
    assert feature_chip_text("40L") == "40L"


def test_chips_cannot_overflow_their_card() -> None:
    from frontend.theme import CSS

    rule = CSS[CSS.index(".ss-chip {"):].split("}", 1)[0]
    assert "max-width: 100%" in rule and "overflow-wrap: anywhere" in rule


def test_client_has_no_profile_methods() -> None:
    profile_methods = {"create_user", "get_user", "set_consent", "get_preferences", "set_preference",
                       "delete_preference", "get_wishlist", "add_to_wishlist", "remove_from_wishlist",
                       "get_feedback", "set_feedback", "clear_feedback", "export_user", "delete_user"}
    assert not profile_methods & set(dir(ShelfSenseClient))
