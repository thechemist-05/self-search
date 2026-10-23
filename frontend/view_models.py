"""Pure presentation logic for the Streamlit UI (Phase 9).

No Streamlit and no network here, so every rule about what the user sees is
unit-testable. Key rules:

- Product facts come only from ``response["products"]`` (database records).
- A claim is shown as supported only if its validation result is SUPPORTED.
  Anything that cannot be matched to a validation result is shown as unverified.
- Invalid support reports always produce a visible warning.
"""

from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

UNVERIFIED_PREFIX = "[UNVERIFIED] "
SUMMARY_LABEL = "AI summary"
DISCLAIMER = "Academic research prototype · not affiliated with any retailer."

# (icon, label, is_supported). Text labels are always shown: status is never colour-only.
CLAIM_STATUS: dict[str, tuple[str, str, bool]] = {
    "SUPPORTED": ("✅", "Supported by cited reviews", True),
    "UNSUPPORTED_LOW_SIMILARITY": ("⚠️", "Not supported by the cited reviews", False),
    "FABRICATED_CITATION": ("⚠️", "Cited evidence not found", False),
    "CROSS_PRODUCT_MISMATCH": ("⚠️", "Cited review is about a different product", False),
    "NUMERIC_MISMATCH": ("⚠️", "Number does not match the catalogue", False),
    "UNVERIFIABLE": ("❔", "Could not be checked against catalogue data", False),
}
UNKNOWN_STATUS = ("⚠️", "Unverified", False)

FALLBACK_REASONS = {
    "provider_unavailable": "the AI model is not running",
    "provider_error": "the AI model did not answer in time",
    "invalid_output_after_retries": "the AI model's reply could not be used",
    "unexpected_error": "the AI model hit an error",
}
RESERVED_IMAGE_DOMAINS = ("example.com", "example.org", "example.net")
RESERVED_IMAGE_TLDS = (".example", ".invalid", ".test", ".localhost")

# Neutral "no image" graphic. Transparent background, so the card's category tint
# shows through; it is never styled to look like a product photo.
_PLACEHOLDER_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' width='320' height='200' viewBox='0 0 320 200'>"
    "<rect x='120' y='56' width='80' height='60' rx='8' fill='none' stroke='#4A4E6B' stroke-width='4' "
    "stroke-opacity='0.55'/>"
    "<circle cx='145' cy='78' r='7' fill='#4A4E6B' fill-opacity='0.55'/>"
    "<path d='M124 112 L150 90 L166 104 L178 94 L196 112 Z' fill='#4A4E6B' fill-opacity='0.55'/>"
    "<text x='160' y='148' font-family='Source Sans, sans-serif' font-size='14' font-weight='600' "
    "fill='#4A4E6B' text-anchor='middle'>Image unavailable</text></svg>"
)
PLACEHOLDER_DATA_URI = "data:image/svg+xml;base64," + base64.b64encode(_PLACEHOLDER_SVG.encode()).decode()


_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|~$#])")
_MD_LINE_START = re.compile(r"(?m)^(\s*)([-+]|\d+\.)(\s)")


def escape_md(text: Any) -> str:
    """Escape Markdown/LaTeX syntax so review and AI text render literally.

    Review snippets are untrusted data; "$5 ... $10" would otherwise render as
    LaTeX, "[x](url)" as a link and "<b>" as markup. List markers are escaped
    only at the start of a line, so ordinary prices like "49.99" stay readable.
    """
    escaped = _MD_SPECIAL.sub(r"\\\1", str(text or ""))
    return _MD_LINE_START.sub(lambda m: f"{m.group(1)}\\{m.group(2)}{m.group(3)}", escaped)


# ---------------------------------------------------------------------------
# Claims and validation
# ---------------------------------------------------------------------------

@dataclass
class ClaimView:
    text: str
    evidence_ids: list[str]
    status: Optional[str]
    icon: str
    label: str
    supported: bool
    missing_evidence_ids: list[str] = field(default_factory=list)


@dataclass
class RecommendationView:
    product_id: int
    why: list[ClaimView]
    watch_outs: list[ClaimView]


def strip_unverified_prefix(text: str) -> str:
    return text[len(UNVERIFIED_PREFIX):] if text.startswith(UNVERIFIED_PREFIX) else text


def annotate_recommendations(response: dict[str, Any]) -> dict[int, RecommendationView]:
    """Attach each answer claim to its validation result.

    Matching is by (product_id, claim text without the ``[UNVERIFIED]`` prefix),
    consuming results in order. A claim with no matching result is unverified.
    """
    answer = response.get("answer") or {}
    report = response.get("support_report") or {}
    known_ids = {e["evidence_id"] for e in response.get("evidence") or []}
    unused = list(report.get("claim_results") or [])

    def view(raw: dict[str, Any], product_id: int) -> ClaimView:
        text = strip_unverified_prefix(raw.get("claim", ""))
        status = None
        for i, res in enumerate(unused):
            if res.get("product_id") == product_id and res.get("claim_text") == text:
                status = res.get("status")
                unused.pop(i)
                break
        icon, label, supported = CLAIM_STATUS.get(status or "", UNKNOWN_STATUS)
        ids = list(raw.get("evidence_ids") or [])
        return ClaimView(text, ids, status, icon, label, supported, [e for e in ids if e not in known_ids])

    out: dict[int, RecommendationView] = {}
    for rec in answer.get("recommendations") or []:
        pid = rec.get("product_id")
        out[pid] = RecommendationView(
            pid,
            [view(c, pid) for c in rec.get("why_it_matches") or []],
            [view(c, pid) for c in rec.get("watch_outs") or []],
        )
    return out


def validation_banner(response: dict[str, Any]) -> Optional[str]:
    """Persistent warning text when an answer's support report is invalid or missing."""
    if not response.get("answer"):
        return None
    report = response.get("support_report")
    if not report:
        return "This answer could not be checked, so treat its claims as unverified."
    if report.get("is_valid"):
        return None
    total, supported = report.get("total_claims", 0), report.get("supported_claims", 0)
    extras = []
    if report.get("fabricated_citations"):
        extras.append(f"{report['fabricated_citations']} cite a missing review")
    if report.get("numeric_mismatches"):
        extras.append(f"{report['numeric_mismatches']} have a number that doesn't match the product")
    if report.get("cross_product_mismatches"):
        extras.append(f"{report['cross_product_mismatches']} cite a review of another product")
    text = f"Only {supported} of {total} claims are backed by the cited reviews"
    text += f" ({'; '.join(extras)})." if extras else "."
    return text + " Unbacked claims are marked ⚠️."


def insufficient_notice(response: dict[str, Any]) -> Optional[str]:
    answer = response.get("answer") or {}
    if not answer.get("insufficient_evidence"):
        return None
    missing = answer.get("missing_information")
    return "Not enough evidence for a reliable recommendation." + (
        f" Missing: {missing}" if missing else ""
    )


def summary_available(response: dict[str, Any]) -> bool:
    """True for a product-first search result that has products but no AI summary yet."""
    gen = response.get("generation") or {}
    return bool(response.get("products")) and gen.get("reason") == "summary_not_requested"


def generation_notice(response: dict[str, Any]) -> Optional[str]:
    """Warning when the answer did not come from the local model."""
    gen = response.get("generation") or {}
    if gen.get("mode") != "fallback":
        return None
    why = FALLBACK_REASONS.get(gen.get("reason") or "", "the AI model was not used")
    return f"Quick answer: {why}, so this summary is built directly from the reviews shown."


# ---------------------------------------------------------------------------
# Product cards
# ---------------------------------------------------------------------------

PRICE_UNAVAILABLE = "Price unavailable"

# Any amount in another currency ("$23.69", "USD 24", "24 dollars", "€10", "£5"). Prices are
# shown only in INR, so such amounts are never displayed (``strip_foreign_prices``).
_AMOUNT = r"[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?"
_FOREIGN_PRICE = re.compile(
    rf"(?:(?:US\s?\$|\$|€|£)\s?(?:{_AMOUNT})"
    rf"|\b(?:USD|EUR|GBP)\s?(?:{_AMOUNT})\b"
    rf"|\b(?:{_AMOUNT})\s?(?:USD|EUR|GBP|US\s?dollars?|dollars?|euros?|pounds\s+sterling)\b)",
    re.IGNORECASE,
)
FOREIGN_AMOUNT_MARK = "[amount removed: prices are shown in ₹]"


def strip_foreign_prices(text: Optional[str]) -> str:
    """Display-time guard: replace any foreign-currency amount with a visible marker.

    The API already sends prices only in INR and removes foreign amounts from answers; this
    also covers source text (titles, features, review quotes) that mentions one. It never
    converts a value and never changes the data used for matching claims to their checks.
    """
    return _FOREIGN_PRICE.sub(FOREIGN_AMOUNT_MARK, text or "")


def inr(amount: float) -> str:
    """Indian digit grouping: "₹799", "₹1,499", "₹12,999", "₹1,25,000"; paise only when present."""
    value = float(amount)
    rupees = int(abs(value))
    paise = round((abs(value) - rupees) * 100)
    if paise == 100:
        rupees, paise = rupees + 1, 0
    digits = str(rupees)
    if len(digits) > 3:
        head, groups = digits[:-3], [digits[-3:]]
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        digits = ",".join(([head] if head else []) + groups)
    return f"{'-' if value < 0 else ''}₹{digits}" + (f".{paise:02d}" if paise else "")


def money(amount: float, currency: Optional[str]) -> str:
    """A price as shown to users: INR only (2026-10-23).

    The API sends every price in INR. Any other currency is never displayed (no symbol
    is swapped and nothing is converted here): it reads "Price unavailable".
    """
    return inr(amount) if (currency or "").upper() == "INR" else PRICE_UNAVAILABLE


def price_label(price: Optional[float], currency: Optional[str]) -> str:
    if price is None:
        return PRICE_UNAVAILABLE
    return money(price, currency)


ESTIMATE_NOTE = "Estimated price · Not a retailer price"
SOURCE_PRICE_NOTE = "Source price"
CONVERTED_PRICE_NOTE = "Converted from source price"
_CURRENCY_WORDS = {"USD": "US dollar", "EUR": "euro", "GBP": "pound sterling"}


def _shows_inr_price(card: dict[str, Any]) -> bool:
    return (card.get("price") is not None and (card.get("currency") or "").upper() == "INR"
            and card.get("price_status") in (None, "verified", "converted"))


def source_price_note(card: dict[str, Any]) -> Optional[str]:
    """Chip beside a source price: "Source price" (INR source) or "Converted from source price"."""
    if not _shows_inr_price(card):
        return None
    return CONVERTED_PRICE_NOTE if card.get("price_status") == "converted" else SOURCE_PRICE_NOTE


def conversion_note(card: dict[str, Any]) -> Optional[str]:
    """How a converted price was obtained, from the API's ``price_conversion`` (no foreign amount)."""
    conv = card.get("price_conversion")
    if card.get("price_status") != "converted" or not conv:
        return None
    unit = _CURRENCY_WORDS.get(conv.get("from_currency", ""), conv.get("from_currency", ""))
    where = "ECB reference rate, " if "European Central Bank" in (conv.get("source") or "") else ""
    return (f"The source lists this price in another currency; it is converted to rupees at "
            f"₹{conv['rate']:.2f} per {unit} ({where}{conv['rate_date']}).")


FEATURE_CHIP_CHARS = 60


def feature_chip_text(feature: str, limit: int = FEATURE_CHIP_CHARS) -> str:
    """A feature shortened for a chip (source features can be whole sentences); never altered otherwise."""
    feature = " ".join(str(feature).split())
    return feature if len(feature) <= limit else feature[: limit - 1].rstrip(" ,;:.") + "…"


def card_price_label(card: dict[str, Any], with_note: bool = False) -> str:
    """The price line, always in rupees: "₹1,968", "≈ ₹2,150" for a synthetic estimate, or
    "Price unavailable".

    Uses only the API's explicit fields (``price``/``currency`` = normalised INR,
    ``price_status``, ``estimated_price``); the currency is never inferred from the number.
    On cards a chip says which kind of price it is; with ``with_note`` (tables, where
    there is no chip) the text itself says so.
    """
    est = card.get("estimated_price")
    if card.get("price_status") == "estimated" and est:
        return f"≈ {money(est['amount'], est.get('currency'))}" + (
            " (estimate, not a retailer price)" if with_note else "")
    if not _shows_inr_price(card):
        return PRICE_UNAVAILABLE
    label = inr(card["price"])
    if with_note:
        label += (" (converted from the source price)" if card.get("price_status") == "converted"
                  else " (source price)")
    return label


def estimate_note(card: dict[str, Any]) -> Optional[str]:
    """Explanation shown under an estimated price; None for source or unknown prices."""
    est = card.get("estimated_price")
    if card.get("price_status") != "estimated" or not est:
        return None
    return (f"The source has no price for this product. This is an illustrative estimate from the "
            f"{money(est['range_min'], est.get('currency'))}–{money(est['range_max'], est.get('currency'))} range for its type "
            f"({est['config_version']}), not a retailer price.")


def rating_label(rating: Optional[float], count: Optional[int]) -> str:
    if rating is None:
        return "No rating yet"
    suffix = f" · {count:,} rating{'s' if count != 1 else ''}" if count else ""
    return f"{rating:.1f}★{suffix}"


def local_reviews_label(n: int) -> str:
    return "No reviews" if not n else f"{n} review{'s' if n != 1 else ''}"


def image_plan(url: Optional[str], load_remote: bool) -> tuple[str, str]:
    """Decide how to show a product image: ("remote", url) or ("placeholder", reason)."""
    if not url:
        return "placeholder", "missing"
    if not load_remote:
        return "placeholder", "remote_images_disabled"
    host = (urlparse(url).hostname or "").lower()
    if urlparse(url).scheme not in ("http", "https") or not host:
        return "placeholder", "invalid_url"
    if host in RESERVED_IMAGE_DOMAINS or host.endswith(tuple("." + d for d in RESERVED_IMAGE_DOMAINS)) \
            or host.endswith(RESERVED_IMAGE_TLDS):
        return "placeholder", "reserved_example_domain"
    return "remote", url


_BAD_STATUSES = {"UNSUPPORTED_LOW_SIMILARITY", "FABRICATED_CITATION", "CROSS_PRODUCT_MISMATCH", "NUMERIC_MISMATCH"}


def claim_tone(status: Optional[str]) -> str:
    """Pill colour job for a claim status: 'ok' (mint), 'bad' (coral) or 'warn' (mustard).

    Anything not explicitly SUPPORTED or contradicted (unverifiable, unmatched) is 'warn',
    never 'ok'.
    """
    if status == "SUPPORTED":
        return "ok"
    return "bad" if status in _BAD_STATUSES else "warn"


def html_text(text: Any) -> str:
    """HTML-escape untrusted text for raw-HTML markdown; '$' is escaped so it can't become LaTeX."""
    return html.escape(str(text or ""), quote=True).replace("$", "&#36;")


def media_html(img: str, accent: str) -> str:
    """Wrap an <img> in the card's category-tinted frame (also marks the card's accent)."""
    return f'<div class="ss-media ss-media-{accent} ss-acc-{accent}">{img}</div>'


def img_html(src: str, alt: str) -> str:
    """Accessible <img> with escaped alt text, scaled to its container."""
    return (
        f'<img src="{html.escape(src, quote=True)}" alt="{html.escape(alt, quote=True)}" '
        'style="width:100%;max-height:200px;object-fit:contain;border-radius:6px;" />'
    )


def card_details(card: dict[str, Any], limit: int = 2) -> list[str]:
    """One or two short facts for the card face: the brand, then the first source features."""
    out = [card["brand"]] if card.get("brand") else []
    out += [feature_chip_text(strip_foreign_prices(f), 40) for f in card.get("features") or []]
    return out[:limit]


def image_alt(card: dict[str, Any], placeholder: bool) -> str:
    title = card.get("title") or "product"
    return f"Image unavailable for {title}" if placeholder else f"Product image of {title}"


def filter_checks(card: dict[str, Any], active_filters: dict[str, Any]) -> list[str]:
    """Which active filters this product satisfies, computed from its database values.

    Fallback for cards without the API's ``constraint_checks`` (question and compare turns).
    Budgets are not checked here: ``active_filters`` carries no currency, so comparing a
    source price with it could set a USD price against a rupee budget. The API's own
    constraint checks, which know both currencies, cover prices.
    """
    out = []
    rating = card.get("catalog_rating")
    if active_filters.get("min_rating") is not None and rating is not None and rating >= active_filters["min_rating"]:
        out.append(f"Catalogue rating {rating:.1f} meets your minimum of {active_filters['min_rating']}")
    return out


_PRICE_PHRASE = re.compile(
    r"\b(?:under|below|less\s+than|up\s*to|max(?:imum)?|budget(?:\s+of|\s+is)?|above|over|more\s+than|"
    r"at\s+least|starting(?:\s+at)?|from|between)\s*(?:[$₹€£]|rs\.?|inr|usd)?\s*\d[\d,.]*"
    r"(?:\s*(?:rupees?|rs\.?|inr|dollars?|usd))?(?:\s*(?:and|to|-)\s*(?:[$₹€£]|rs\.?|inr|usd)?\s*\d[\d,.]*"
    r"(?:\s*(?:rupees?|rs\.?|inr|dollars?|usd))?)?", re.IGNORECASE)
_RATING_PHRASE = re.compile(
    r"\b(?:\d(?:\.\d)?\+?\s*(?:stars?|rating|rated)|rated\s+(?:above|over|at\s+least)\s*\d(?:\.\d)?|"
    r"highly\s+rated|top[- ]rated|best\s+reviewed|well[- ]reviewed)\b", re.IGNORECASE)


def _without(text: str, phrase: str) -> str:
    out = re.sub(rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])", " ", text, flags=re.IGNORECASE)
    return " ".join(out.split()).strip(" ,;")


def broaden_options(question: str, response: dict[str, Any]) -> list[tuple[str, str]]:
    """(button label, broader search text) for a search with no exact matches.

    Each option is a *new, separately labelled search* with one requirement removed;
    alternatives are never mixed into exact results. The product type is never dropped.
    """
    if response.get("products") or not response.get("constraints"):
        return []
    options: list[tuple[str, str]] = []
    for item in response["constraints"].get("items") or []:
        if item.get("status") != "applied":
            continue
        kind, value = item.get("kind"), item.get("value")
        if kind in ("colour", "material", "occasion", "brand") and value:
            broader = _without(question, str(value))
            label = {"colour": f"Any colour (not just {value})", "material": f"Any material (not just {value})",
                     "occasion": f"Any occasion (not just {value})",
                     "brand": f"Any brand (not just {value})"}[kind]
        elif kind == "price":
            broader, label = " ".join(_PRICE_PHRASE.sub(" ", question).split()), "Any price"
        elif kind == "min_rating":
            broader, label = " ".join(_RATING_PHRASE.sub(" ", question).split()), "Any rating"
        else:
            continue
        if broader and broader.casefold() != question.casefold():
            options.append((label, broader))
    return options


def unknown_attribute_note(response: dict[str, Any]) -> Optional[str]:
    """Why products of the right kind weren't shown: their listing doesn't state the colour/material/occasion."""
    report = response.get("constraints") or {}
    n = report.get("unknown_attribute_products")
    if not n:
        return None
    kinds = sorted({i["kind"] for i in report.get("items") or []
                    if i.get("kind") in ("colour", "material", "occasion")})
    what = " or ".join(kinds) or "attribute"
    return (f"{n:,} other product{'s' if n != 1 else ''} match everything else but don't state their {what}, "
            "so they aren't shown as exact matches.")


@dataclass
class FilterChip:
    label: str
    clear: list[str]   # session filter names a click removes; empty = not removable (follows each search)


def filter_row(response: Optional[dict[str, Any]]) -> list[FilterChip]:
    """Short chips for the filters applied to this answer ("Footwear", "Under ₹3,000", "4+ stars").

    Built from the response's constraint report, so it shows exactly what was enforced.
    Price, rating, brand and category stay in the conversation until removed; product
    types and exclusions follow each new search, so they are not removable.
    """
    chips: list[FilterChip] = []
    for item in ((response or {}).get("constraints") or {}).get("items") or []:
        if item.get("status") != "applied":
            continue
        kind, value = item.get("kind"), item.get("value")
        if kind == "product_type":
            types = value if isinstance(value, list) else [value]
            label = " / ".join(str(t).capitalize() for t in types if t)
            if label:
                chips.append(FilterChip(label, []))
        elif kind == "price" and isinstance(value, dict):
            lo, hi, cur = value.get("min"), value.get("max"), (value.get("currency") or "INR").upper()
            if cur != "INR":
                continue  # budgets are INR; any other currency is never shown
            if lo is not None and hi is not None:
                chips.append(FilterChip(f"{inr(lo)}–{inr(hi)}", ["min_price", "max_price"]))
            elif hi is not None:
                chips.append(FilterChip(f"Under {inr(hi)}", ["max_price"]))
            elif lo is not None:
                chips.append(FilterChip(f"From {inr(lo)}", ["min_price"]))
        elif kind == "min_rating" and value is not None:
            chips.append(FilterChip(f"{float(value):g}+ stars", ["min_rating"]))
        elif kind in ("brand", "category") and value:
            chips.append(FilterChip(str(value).replace("_", " "), [kind]))
        elif kind in ("colour", "material", "occasion") and value:
            chips.append(FilterChip(str(value).capitalize(), []))
        elif kind == "excluded_brand" and value:
            chips.append(FilterChip(f"Not {value}", []))
        elif kind == "excluded_term" and value:
            chips.append(FilterChip(f"Without {value}", []))
    return chips


@dataclass
class ConstraintView:
    applied: list[str]
    not_applied: list[tuple[str, str]]   # (label, reason)
    matching_products: Optional[int]
    catalog_currencies: list[str]


def constraint_view(response: dict[str, Any]) -> Optional[ConstraintView]:
    """How the request's hard constraints were interpreted, for the 'Your request' panel."""
    report = response.get("constraints")
    if not report:
        return None
    items = report.get("items") or []
    return ConstraintView(
        applied=[i["label"] for i in items if i.get("status") == "applied"],
        not_applied=[(i["label"], i.get("reason") or "") for i in items if i.get("status") != "applied"],
        matching_products=report.get("matching_products"),
        catalog_currencies=report.get("catalog_currencies") or [],
    )


def compare_rows(products: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Rows for a side-by-side table of database facts."""
    rows = []
    for label, fn in [
        ("Price", lambda p: card_price_label(p, with_note=True)),
        ("Rating", lambda p: rating_label(p.get("catalog_rating"), p.get("catalog_rating_count"))),
        ("Reviews", lambda p: local_reviews_label(p.get("local_review_count", 0))),
        ("Brand", lambda p: p.get("brand") or "—"),
        ("Features", lambda p: "; ".join(feature_chip_text(strip_foreign_prices(f), 60)
                                         for f in (p.get("features") or [])[:3]) or "—"),
    ]:
        row = {"Attribute": label}
        for p in products:
            row[strip_foreign_prices(p.get("title", str(p.get("id"))))] = fn(p)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

ERROR_MESSAGES = {
    "unreachable": "Self Search is not reachable. Check that the backend is running, then try again.",
    "timeout": ("The answer took too long, so the request was stopped. Self Search is running, but the AI "
                "model is slow on this computer. Please try again."),
    "auth": "The API rejected the request (invalid or missing API key). Check SHELFSENSE_API_KEY for the UI.",
    "not_found": "Not found.",
    "conflict": "This conversation could not be continued. A new conversation has been started.",
    "validation": "The request was not accepted",
    "unavailable": "The service is temporarily unavailable. Please try again.",
    "server": "The server hit an error while handling this request.",
    "other": "The request failed.",
}


def error_message(kind: str, detail: str = "") -> str:
    base = ERROR_MESSAGES.get(kind, ERROR_MESSAGES["other"])
    if kind in ("validation", "not_found", "other", "server") and detail:
        return f"{base}: {detail}" if not base.endswith(".") else f"{base} {detail}"
    return base
