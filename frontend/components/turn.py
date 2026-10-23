"""Rendering of one assistant answer: short notices, the AI summary, product cards,
review evidence and the comparison table.

Cards show database values only (image, name, price, rating, one or two details).
Evidence warnings and synthetic-price labels are kept, compactly; nothing that
prevents a misleading reading is hidden.
"""

from __future__ import annotations

import base64
import html
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import streamlit as st

from frontend import state
from frontend.theme import category_accent, chip, chips
from frontend.view_models import (
    ESTIMATE_NOTE,
    PLACEHOLDER_DATA_URI,
    SUMMARY_LABEL,
    ClaimView,
    RecommendationView,
    annotate_recommendations,
    card_details,
    card_price_label,
    claim_tone,
    compare_rows,
    conversion_note,
    constraint_view,
    escape_md,
    estimate_note,
    filter_checks,
    generation_notice,
    html_text,
    image_alt,
    image_plan,
    img_html,
    insufficient_notice,
    local_reviews_label,
    media_html,
    rating_label,
    source_price_note,
    strip_foreign_prices,
    validation_banner,
)

CARDS_PER_ROW = 3
MAX_CARD_CLAIMS = 2

# url -> (fetched_at, result). Failures are cached briefly so an offline machine
# doesn't re-try every image on every rerun, but a transient failure recovers.
_IMAGE_CACHE: dict[str, tuple[float, Optional[tuple[bytes, str]]]] = {}
_IMAGE_TTL_OK, _IMAGE_TTL_FAIL, _IMAGE_CACHE_MAX = 3600.0, 300.0, 200


def clear_image_cache() -> None:
    _IMAGE_CACHE.clear()


def _remote_image(url: str, client: Any) -> Optional[tuple[bytes, str]]:
    now = time.monotonic()
    hit = _IMAGE_CACHE.get(url)
    if hit is not None and now - hit[0] < (_IMAGE_TTL_OK if hit[1] else _IMAGE_TTL_FAIL):
        return hit[1]
    result = client.fetch_image(url)
    if len(_IMAGE_CACHE) >= _IMAGE_CACHE_MAX:
        _IMAGE_CACHE.pop(next(iter(_IMAGE_CACHE)))
    _IMAGE_CACHE[url] = (now, result)
    return result


def prefetch_images(cards: list[dict[str, Any]], client: Any, load_remote: bool) -> None:
    """Fetch every card's image in parallel before rendering, so N slow or unreachable images
    cost one timeout (5 s), not N. Results land in the cache; failures fall back to the placeholder."""
    urls = [u for c in cards for kind, u in [image_plan(c.get("image_url"), load_remote)] if kind == "remote"]
    now = time.monotonic()
    todo = [u for u in dict.fromkeys(urls)
            if not (u in _IMAGE_CACHE and now - _IMAGE_CACHE[u][0] < (_IMAGE_TTL_OK if _IMAGE_CACHE[u][1] else _IMAGE_TTL_FAIL))]
    if len(todo) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
            list(pool.map(lambda u: _remote_image(u, client), todo))


def _h(text: Any) -> str:
    return html.escape(str(text or ""), quote=True)


# ---------------------------------------------------------------------------
# Answer
# ---------------------------------------------------------------------------

def render_turn(index: int, response: dict[str, Any]) -> None:
    answer = response.get("answer")

    if response.get("smalltalk_response"):
        st.markdown(escape_md(response["smalltalk_response"]))
    if response.get("clarification_question"):
        st.info(response["clarification_question"], icon="💬")
    view = constraint_view(response)
    for label, reason in (view.not_applied if view else []):
        st.warning(f"Not applied: {label}. {reason}".strip(), icon="⚠️")

    if not answer:
        return

    # Notices that prevent a misleading reading stay visible (never inside an expander).
    notice = generation_notice(response)
    if notice:
        st.warning(notice, icon="🔌")
    banner = validation_banner(response)
    if banner:
        st.error(banner, icon="⚠️")
    insufficient = insufficient_notice(response)
    if insufficient:
        st.warning(insufficient, icon="ℹ️")

    summary = answer.get("summary", "")
    products = response.get("products") or []
    if summary and products:
        st.markdown(f'<div class="ss-summary"><span class="ss-label">{_h(SUMMARY_LABEL)}</span>'
                    f'<p>{html_text(strip_foreign_prices(summary))}</p></div>', unsafe_allow_html=True)

    if products:
        _cards(index, response, products)
    elif response.get("result_product_ids"):
        st.warning("Product details could not be loaded for this answer.")
    else:
        st.info("No products match. Try removing a filter or describing what you need differently.", icon="🔎")

    if response.get("intent") == "COMPARE" and len(products) >= 2:
        st.markdown('<div class="ss-section">Comparison</div>', unsafe_allow_html=True)
        st.table(compare_rows(products))


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def _cards(index: int, response: dict[str, Any], products: list[dict[str, Any]]) -> None:
    prefetch_images(products, state.client(), st.session_state["config"].load_remote_images)
    recs = annotate_recommendations(response)
    evidence = {e["evidence_id"]: e for e in response.get("evidence") or []}
    for start in range(0, len(products), CARDS_PER_ROW):
        cols = st.columns(CARDS_PER_ROW, gap="medium")
        for col, card in zip(cols, products[start:start + CARDS_PER_ROW]):
            with col:
                _card(index, card, recs.get(card["id"]), evidence, response.get("active_filters") or {})


def _image(card: dict[str, Any], accent: str) -> None:
    """The product's own image when remote images are on and it loads; else another validated
    photo of the same product (``image_alternates``); otherwise the neutral "Image unavailable"
    placeholder. Never another product's image."""
    cfg = st.session_state["config"]
    for url in [card.get("image_url"), *(card.get("image_alternates") or [])]:
        kind, value = image_plan(url, cfg.load_remote_images)
        if kind != "remote":
            continue
        fetched = _remote_image(value, state.client())
        if fetched is not None:
            data, mime = fetched
            src = f"data:{mime};base64,{base64.b64encode(data).decode()}"
            st.markdown(media_html(img_html(src, image_alt(card, placeholder=False)), accent), unsafe_allow_html=True)
            return
    st.markdown(media_html(img_html(PLACEHOLDER_DATA_URI, image_alt(card, placeholder=True)), accent),
                unsafe_allow_html=True)


def _claim_line(c: ClaimView, compact: bool = False) -> str:
    """Claim with its status as a worded pill (colour is never the only signal)."""
    label = ("Backed by reviews" if c.supported else "Not verified") if compact else c.label
    cites = ", ".join(c.evidence_ids) if c.evidence_ids else "no citation"
    return (
        f'<div class="ss-claim">{chip(c.icon + " " + html_text(label), claim_tone(c.status))} '
        f'{html_text(strip_foreign_prices(c.text))} <span class="ss-cite">[{html_text(cites)}]</span></div>'
    )


def _card(
    index: int,
    card: dict[str, Any],
    rec: Optional[RecommendationView],
    evidence: dict[str, dict[str, Any]],
    active_filters: dict[str, Any],
) -> None:
    pid, title = card["id"], strip_foreign_prices(card["title"])
    accent = category_accent(card.get("category"))
    with st.container(border=True, key=f"card-{index}-{pid}"):
        _image(card, accent)
        if card.get("recommended"):
            st.markdown('<span class="ss-badge">Top pick</span>', unsafe_allow_html=True)
        # Full name in the tooltip; the card shows two lines with an ellipsis.
        st.markdown(f'<div class="ss-title" role="heading" aria-level="4" title="{html_text(title)}">{html_text(title)}</div>',
                    unsafe_allow_html=True)

        price = f'<div class="ss-price">{html_text(card_price_label(card))}</div>'
        if card.get("price_status") == "estimated" and card.get("estimated_price"):
            price += chip("≈ " + _h(ESTIMATE_NOTE), "warn")
        elif source_price_note(card):
            price += chip(_h(source_price_note(card)))
        st.markdown(price, unsafe_allow_html=True)

        rating = rating_label(card.get("catalog_rating"), card.get("catalog_rating_count"))
        reviews = local_reviews_label(card.get("local_review_count", 0))
        st.markdown(f'<div class="ss-meta">{html_text(rating)} · {html_text(reviews)}</div>', unsafe_allow_html=True)
        details = card_details(card)
        if details:
            st.markdown(f'<div class="ss-details">{" · ".join(html_text(d) for d in details)}</div>',
                        unsafe_allow_html=True)

        for c in (rec.why if rec else [])[:MAX_CARD_CLAIMS]:
            st.markdown(_claim_line(c, compact=True), unsafe_allow_html=True)

        a, b = st.columns(2, vertical_alignment="center")
        a.button("Ask about this", key=f"ask-{index}-{pid}", on_click=state.ask_about, args=(pid, title),
                 help=f"Ask a question about {title}", width="stretch", icon=":material/chat:")
        cmp_key = f"cmp-{index}-{pid}"
        b.checkbox("Compare", key=cmp_key, value=pid in st.session_state["compare_selection"],
                   on_change=state.toggle_compare, args=(pid, cmp_key), help=f"Add {title} to the comparison")

        with st.expander("Reviews and details"):
            _details(index, card, rec, evidence, active_filters)


def _details(
    index: int,
    card: dict[str, Any],
    rec: Optional[RecommendationView],
    evidence: dict[str, dict[str, Any]],
    active_filters: dict[str, Any],
) -> None:
    claims = (rec.why + rec.watch_outs) if rec else []
    if claims:
        st.markdown('<div class="ss-label">What the AI said</div>', unsafe_allow_html=True)
        for c in claims:
            st.markdown(_claim_line(c), unsafe_allow_html=True)
            for eid in c.evidence_ids:
                item = evidence.get(eid)
                if item is None:
                    st.caption(f"{escape_md(eid)}: cited review not found.")
                elif item["product_id"] != card["id"]:
                    st.caption(f"{escape_md(eid)} is a review of a different product.")

    items = [e for e in evidence.values() if e["product_id"] == card["id"]]
    if items:
        st.markdown('<div class="ss-label">Reviews used</div>', unsafe_allow_html=True)
        for item in items:
            stars = f"{item['rating']:.0f}★ " if item.get("rating") is not None else ""
            verified = " · verified purchase" if item.get("verified") else ""
            st.caption(f"{escape_md(item['evidence_id'])} · {stars}{verified}")
            st.markdown("> " + escape_md(strip_foreign_prices(item["text"])))

    note = estimate_note(card) or conversion_note(card)
    if note:
        st.caption(escape_md(note))
    checks = [c["detail"] for c in card.get("constraint_checks") or []] or filter_checks(card, active_filters)
    if checks:
        st.caption("Matches: " + escape_md(" · ".join(checks)))

    detail = st.session_state["details"].get(card["id"])
    if detail is None:
        st.button("Show all reviews", key=f"detail-{index}-{card['id']}", on_click=state.load_details,
                  args=(card["id"],), icon=":material/reviews:")
        return
    reviews = detail.get("reviews") or []
    if not reviews:
        st.caption("No reviews for this product.")
    for r in reviews:
        stars = f"{r['rating']:.0f}★" if r.get("rating") is not None else "unrated"
        verified = " · verified purchase" if r.get("verified") else ""
        st.caption(f"{stars}{verified} · {r.get('review_date') or 'undated'}")
        st.markdown("> " + escape_md(strip_foreign_prices(r.get("text", ""))))
