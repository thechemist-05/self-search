"""Session state and user actions for the Streamlit UI (Phase 9).

Widget callbacks only *queue* a chat request (``queue``); the page runs it with
``run_pending`` under one loading indicator, then renders the answer. Nothing is
saved as a side effect of rendering. There are no user profiles: a conversation
lives only in this browser tab and the API session.

Searches return products without a model call. The optional AI summary of a
result set runs in a background thread (``request_summary``), so the results stay
on screen and usable while it is generated; ``poll_summary`` picks up the outcome.
"""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

import streamlit as st

from frontend.api_client import ApiError, ShelfSenseClient
from frontend.config import FrontendConfig
from frontend.view_models import error_message

_DEFAULTS: dict[str, Any] = {
    "messages": [],            # {"role": "user"|"assistant"|"error", "text": str, "response": dict|None}
    "session_id": None,
    "compare_selection": [],   # product IDs ticked for comparison, in tick order
    "ask_about": None,         # {"id": int, "title": str} for the next question
    "focus_request": 0,        # bumped by "Ask about this": move the user to the search box once
    "focus_done": 0,
    "flash": [],               # (level, text) shown once
    "details": {},             # product_id -> GET /products/{id}
    "pending": None,           # (text, product_ids) queued by a widget, sent by run_pending()
    "health": None,            # (checked_at, /health result or None)
    "summaries": {},           # reply message index -> {"future": Future|None, "error": str|None}
}
HEALTH_TTL_S = 30.0  # unrelated reruns (ticking a box, opening details) don't re-query /health
# Summaries run off the script thread; the model serialises them anyway, so two workers suffice.
SUMMARY_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="shelfsense-summary")


def init_state() -> None:
    for key, value in _DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = value.copy() if hasattr(value, "copy") else value
    if "config" not in st.session_state:
        st.session_state["config"] = FrontendConfig.from_env()
    if "client" not in st.session_state:
        st.session_state["client"] = ShelfSenseClient(st.session_state["config"])


def client() -> ShelfSenseClient:
    return st.session_state["client"]


def health() -> Optional[dict[str, Any]]:
    """GET /health, cached for ``HEALTH_TTL_S``; None when the API is unreachable (failures aren't cached)."""
    cached = st.session_state.get("health")
    if cached and cached[1] is not None and time.monotonic() - cached[0] < HEALTH_TTL_S:
        return cached[1]
    try:
        result = client().health()
    except ApiError:
        result = None
    st.session_state["health"] = (time.monotonic(), result)
    return result


def new_search() -> None:
    """Start over: clear the conversation, selections and the server session reference."""
    ss = st.session_state
    ss["messages"], ss["session_id"], ss["compare_selection"] = [], None, []
    ss["ask_about"], ss["details"], ss["pending"] = None, {}, None
    ss["summaries"] = {}  # a summary still running for the old results is discarded when it ends


def flash(level: str, text: str) -> None:
    st.session_state["flash"].append((level, text))


def _guard(fn: Callable[[], Any], on_error: Optional[Callable[[ApiError], None]] = None) -> Any:
    """Run an API action; turn ApiError into a visible message instead of a crash."""
    try:
        return fn()
    except ApiError as e:
        if on_error:
            on_error(e)
        flash("error", error_message(e.kind, e.detail))
        return None


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

def send_message(text: str, product_ids: Optional[list[int]] = None) -> None:
    """Send one chat message (optionally about selected products) and record the reply."""
    text = (text or "").strip()
    if not text:
        return
    ss = st.session_state
    ss["messages"].append({"role": "user", "text": text, "response": None})

    def on_error(e: ApiError) -> None:
        if e.kind == "conflict":
            ss["session_id"] = None
        ss["messages"].append({"role": "error", "text": error_message(e.kind, e.detail), "response": None})

    try:
        resp = client().chat(text, ss["session_id"], product_ids)
    except ApiError as e:
        on_error(e)
        return
    ss["session_id"] = resp["session_id"]
    ss["messages"].append({"role": "assistant", "text": "", "response": resp})
    ss["ask_about"] = None
    if product_ids and len(product_ids) > 1:
        ss["compare_selection"] = []


def queue(text: str, product_ids: Optional[list[int]] = None) -> None:
    """Queue one chat request; ``run_pending`` sends it on this rerun."""
    if (text or "").strip():
        st.session_state["pending"] = (text, product_ids)


def run_pending() -> bool:
    """Send the queued request, if any. Returns True if one was sent."""
    pending = st.session_state.get("pending")
    if not pending:
        return False
    st.session_state["pending"] = None
    send_message(*pending)
    return True


def submit_from(widget_key: str) -> None:
    """Queue the search box's text; while a product is selected it is a question about it."""
    ask = st.session_state.get("ask_about")
    queue(st.session_state.get(widget_key) or "", [ask["id"]] if ask else None)


def compare_selected() -> None:
    queue("Compare the selected products", list(st.session_state["compare_selection"]))


def toggle_compare(product_id: int, widget_key: str) -> None:
    selection = st.session_state["compare_selection"]
    if st.session_state.get(widget_key):
        if product_id not in selection:
            selection.append(product_id)
    elif product_id in selection:
        selection.remove(product_id)


def ask_about(product_id: int, title: str) -> None:
    """Make the next search-box message a question about this product, and bring the box into view
    (the button sits on a card far below it, so otherwise nothing visible changes)."""
    st.session_state["ask_about"] = {"id": product_id, "title": title}
    st.session_state["focus_request"] = st.session_state.get("focus_request", 0) + 1


def cancel_ask_about() -> None:
    st.session_state["ask_about"] = None


_KIND_OF_FILTER = {"min_price": "price", "max_price": "price", "min_rating": "min_rating",
                   "brand": "brand", "category": "category"}


def clear_filter(*names: str) -> None:
    """Remove session filters (e.g. both price bounds of a range); applies from the next search."""
    sid = st.session_state["session_id"]
    if not sid:
        return
    for name in names:
        if _guard(lambda name=name: client().clear_filter(sid, name)) is None:
            return
    last = latest_search_response()
    if last is not None:
        for name in names:
            (last.get("active_filters") or {})[name] = None
        kinds = {_KIND_OF_FILTER.get(n) for n in names}
        report = last.get("constraints") or {}
        report["items"] = [i for i in report.get("items") or [] if i.get("kind") not in kinds]
    flash("success", "Filter removed. It applies to your next search.")


# ---------------------------------------------------------------------------
# Optional AI summary of a search's results
# ---------------------------------------------------------------------------

def request_summary(index: int) -> None:
    """Start the AI summary for the search reply at ``index``, unless one is already running.

    Repeated clicks and reruns while it runs start nothing new; the API also returns a
    cached summary for the same results instead of calling the model again.
    """
    ss = st.session_state
    job = ss["summaries"].get(index)
    if job and job.get("future") is not None and not job["future"].done():
        return
    try:
        response = ss["messages"][index]["response"] or {}
    except (IndexError, KeyError):
        return
    sid, ids = ss.get("session_id"), response.get("result_product_ids")
    if not sid or not ids:
        return
    future: Future = SUMMARY_EXECUTOR.submit(client().summary, sid, list(ids))
    ss["summaries"][index] = {"future": future, "error": None}


def poll_summary(index: int) -> Optional[str]:
    """State of the summary for reply ``index``: None, "running", "done" or "error".

    On success the reply is replaced by the summary response, which carries the same
    products in the same order; a summary of results no longer on screen is discarded.
    """
    ss = st.session_state
    job = ss["summaries"].get(index)
    if not job:
        return None
    future = job.get("future")
    if future is None:
        return "error" if job.get("error") else None
    if not future.done():
        return "running"
    try:
        result = future.result()
    except ApiError as e:
        job["future"], job["error"] = None, error_message(e.kind, e.detail)
        return "error"
    except Exception as e:  # never let a failed summary break the results on screen
        job["future"], job["error"] = None, error_message("other", str(e))
        return "error"
    del ss["summaries"][index]
    try:
        msg = ss["messages"][index]
    except IndexError:
        return None
    if (msg.get("response") or {}).get("result_product_ids") == result.get("result_product_ids"):
        msg["response"] = result
    return "done"


def load_details(product_id: int) -> None:
    detail = _guard(lambda: client().product(product_id))
    if detail is not None:
        st.session_state["details"][product_id] = detail


def latest_response() -> Optional[dict[str, Any]]:
    for msg in reversed(st.session_state["messages"]):
        if msg["role"] == "assistant":
            return msg["response"]
    return None


def latest_search_response() -> Optional[dict[str, Any]]:
    """The latest answer with a constraint report (a search), for the filter row."""
    for msg in reversed(st.session_state["messages"]):
        if msg["role"] == "assistant" and (msg["response"] or {}).get("constraints"):
            return msg["response"]
    return None


def exchanges() -> list[tuple[int, str, dict[str, Any]]]:
    """(reply's message index, user text, reply) triples, oldest first.

    A reply is an assistant or error message; the index keeps widget keys stable.
    """
    out, question = [], None
    for i, msg in enumerate(st.session_state["messages"]):
        if msg["role"] == "user":
            question = msg["text"]
        elif question is not None:
            out.append((i, question, msg))
            question = None
    return out
