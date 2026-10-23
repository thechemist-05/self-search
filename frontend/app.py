# frontend/app.py
"""Self Search shopping assistant UI.

Run:  streamlit run frontend/app.py
Talks to the Self Search API at SHELFSENSE_API_URL (default http://127.0.0.1:8000)
using only its documented JSON contract. Product facts come from the API's
database-backed ``products``; claims are shown with their validation status.

Layout: compact header (brand + New search), a short hero before the first search,
one search box (searches and follow-up questions), active-filter chips, the latest
answer with its exact matches, earlier answers on request, one footer disclaimer.
No sidebar.

Searches show their products as soon as retrieval finishes (no model call). The AI
summary is optional: "Summarise reviews" runs it in the background while the
results stay usable; questions and comparisons are answered by the model on demand.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `streamlit run frontend/app.py` puts only frontend/ on sys.path; make `frontend.*` importable.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import streamlit as st  # noqa: E402

from frontend import state  # noqa: E402
from frontend.components.turn import render_turn  # noqa: E402
from frontend.theme import CSS  # noqa: E402
from frontend.view_models import (  # noqa: E402
    DISCLAIMER,
    broaden_options,
    filter_row,
    html_text,
    summary_available,
    unknown_attribute_note,
)

# Examples for the Fashion catalogue (Amazon Reviews 2023), including the Indian ethnic wear
# and earrings added on 2026-10-23. Budgets are in rupees, like every price shown.
EXAMPLES = [
    "running shoes under ₹3000",
    "red silk saree under ₹3,000",
    "bridal jhumkas under ₹1,500",
    "festive lehenga",
    "black heels",
    "men's leather belt",
]
SEARCH_INTENTS = ("PRODUCT_SEARCH", "REFINE_SEARCH")
SUMMARY_POLL_S = 2.0

st.set_page_config(page_title="Self Search", page_icon="🛍️", layout="wide", initial_sidebar_state="collapsed")
st.markdown(CSS, unsafe_allow_html=True)
state.init_state()
ss = st.session_state
health = state.health()

# ---- Header ------------------------------------------------------------------
brand, action = st.columns([6, 1], vertical_alignment="center")
brand.markdown('<div class="ss-brand">Self <span>Search</span></div>', unsafe_allow_html=True)
if ss["messages"] or ss["pending"]:
    action.button("New search", key="new-search", on_click=state.new_search, icon=":material/refresh:",
                  width="stretch")
st.markdown('<div class="ss-rule"></div>', unsafe_allow_html=True)

# ---- Hero (only before the first search) ---------------------------------------
if not ss["messages"] and not ss["pending"]:
    st.markdown(
        '<div class="ss-eyebrow">Product discovery · backed by real reviews</div>'
        '<h1 class="ss-hero-title">Find exactly what you have in mind.</h1>'
        '<p class="ss-hero-sub">Describe it in your own words, with colour, budget or brand, and see only '
        "the products that match, with the customer reviews behind every claim.</p>",
        unsafe_allow_html=True,
    )

# ---- Search ------------------------------------------------------------------
asking = ss["ask_about"]
with st.form("hero-search", clear_on_submit=True, border=False):
    q, go = st.columns([6, 1], vertical_alignment="bottom")
    q.text_input(
        "Search", key="hero-query", label_visibility="collapsed",
        placeholder=(f"Ask about {asking['title'][:60]}…" if asking
                     else "Try “red silk saree”, “bridal jhumkas under ₹1,500” or “black heels”"),
    )
    go.form_submit_button("Search", type="primary", on_click=state.submit_from, args=("hero-query",),
                          disabled=health is None, icon=":material/search:", width="stretch")

if asking:
    cols = st.columns([5, 1], vertical_alignment="center")
    cols[0].markdown(f'<div class="ss-asking">Asking about <b>{html_text(asking["title"])}</b></div>',
                     unsafe_allow_html=True)
    cols[1].button("Cancel", key="cancel-ask", on_click=state.cancel_ask_about, icon=":material/close:")
    if ss["focus_request"] != ss["focus_done"]:
        # Once per click: scroll the search box and this banner into view and put the cursor in the box.
        ss["focus_done"] = ss["focus_request"]
        st.html(
            f"<script data-request='{ss['focus_request']}'>setTimeout(() => {{"
            "const box = document.querySelector('.st-key-hero-query input');"
            "if (!box) return;"
            "const smooth = !matchMedia('(prefers-reduced-motion: reduce)').matches;"
            "box.scrollIntoView({block: 'center', behavior: smooth ? 'smooth' : 'auto'});"
            "box.focus({preventScroll: true});"
            "}, 60);</script>",
            unsafe_allow_javascript=True,
        )

if not ss["messages"] and not ss["pending"]:
    cols = st.columns(len(EXAMPLES))
    for i, (col, example) in enumerate(zip(cols, EXAMPLES)):
        col.button(example, key=f"example-{i}", on_click=state.queue, args=(example,), disabled=health is None)

# ---- A queued request runs here, with loading feedback for that request only ----
if ss["pending"]:
    loading = st.empty()
    with loading.container():
        if ss["pending"][1]:  # a question about, or comparison of, selected products: a model answer
            st.markdown('<p class="ss-progress">The local AI model is reading the reviews. '
                        "This usually takes under a minute.</p>", unsafe_allow_html=True)
        else:
            st.markdown('<p class="ss-progress">Finding matching products…</p>'
                        '<div class="ss-loading">' + '<div class="ss-skel"><i></i><i></i><i></i></div>' * 3
                        + "</div>", unsafe_allow_html=True)
    with st.spinner("Working…"):
        state.run_pending()
    loading.empty()

# ---- Active filters ------------------------------------------------------------
chip_row = filter_row(state.latest_search_response())
if chip_row:
    cols = st.columns(len(chip_row), gap="small")
    for i, (col, fc) in enumerate(zip(cols, chip_row)):
        if fc.clear:
            col.button(f"{fc.label} ✕", key=f"chip-{'-'.join(fc.clear)}", on_click=state.clear_filter,
                       args=tuple(fc.clear), help=f"Remove “{fc.label}”")
        else:
            col.markdown(f'<span class="ss-filter">{html_text(fc.label)}</span>', unsafe_allow_html=True)

# ---- Problems only -------------------------------------------------------------
if health is None:
    st.error("Self Search is not reachable. Start the backend, then reload this page.", icon="🚫")
elif health.get("ollama") != "available":
    st.warning("The AI model is offline, so answers are built directly from the reviews.", icon="🔌")

while ss["flash"]:
    level, text = ss["flash"].pop(0)
    getattr(st, level if level in ("error", "success", "info", "warning") else "info")(text)


@st.fragment(run_every=SUMMARY_POLL_S)
def _summary_progress(index: int) -> None:
    """Polls only this small area while the summary runs; the rest of the page stays usable."""
    if state.poll_summary(index) == "running":
        st.markdown('<p class="ss-progress">Summarising reviews of the top matches…</p>',
                    unsafe_allow_html=True)
    else:
        st.rerun()  # finished or failed: redraw the page once with the outcome


def _summary_action(index: int, response: dict, status: object) -> None:
    """The optional AI summary: a button, its progress while it runs, or a retry after a failure."""
    if status == "running":
        _summary_progress(index)
        return
    if status == "error":
        st.warning(f"The summary could not be generated. {ss['summaries'][index]['error']}", icon="⚠️")
        st.button("Try again", key=f"summarise-retry-{index}", on_click=state.request_summary, args=(index,),
                  icon=":material/refresh:")
        return
    if summary_available(response):
        st.button("Summarise reviews", key=f"summarise-{index}", on_click=state.request_summary, args=(index,),
                  icon=":material/auto_awesome:", help="The local AI model summarises reviews of the top matches")


def _exchange(index: int, question: str, reply: dict, latest: bool) -> None:
    # Poll first: a finished summary replaces this reply's response before it is drawn.
    status = state.poll_summary(index) if latest else None
    response = reply.get("response") or {}
    count = ""
    if reply["role"] != "error" and response.get("intent") in SEARCH_INTENTS and response.get("answer"):
        n = len(response.get("products") or [])
        count = f"{n} exact match{'es' if n != 1 else ''}" if n else "No exact matches"
    st.markdown(f'<div class="ss-results-head"><span class="ss-query">{html_text(question)}</span>'
                f'<span class="ss-count">{html_text(count)}</span></div>', unsafe_allow_html=True)
    if reply["role"] == "error":
        st.error(reply["text"])
        return
    if latest:
        _summary_action(index, response, status)
    render_turn(index, response)
    note = unknown_attribute_note(response)
    if note:
        st.caption(note)
    options = broaden_options(question, response) if latest else []
    if options:
        st.markdown('<div class="ss-section">Broaden the search</div>', unsafe_allow_html=True)
        cols = st.columns(len(options), gap="small")
        for i, (col, (label, broader)) in enumerate(zip(cols, options)):
            col.button(label, key=f"broaden-{index}-{i}", on_click=state.queue, args=(broader,),
                       help=f"New search: “{broader}”")


# ---- Latest answer, then earlier ones -------------------------------------------
history = state.exchanges()
if history:
    _exchange(*history[-1], latest=True)

    n = len(ss["compare_selection"])
    if n:
        st.button(f"Compare selected ({n})", key="compare-selected", on_click=state.compare_selected,
                  disabled=not 2 <= n <= 4, help="Select 2 to 4 products to compare", type="primary",
                  icon=":material/compare_arrows:")
        if n > 4:
            st.caption("You can compare at most 4 products.")

    earlier = history[:-1]
    if earlier and st.toggle(f"Show earlier results ({len(earlier)})", key="show-earlier"):
        for item in reversed(earlier):
            st.divider()
            _exchange(*item, latest=False)

st.markdown(f'<div class="ss-footer">{html_text(DISCLAIMER)}</div>', unsafe_allow_html=True)
