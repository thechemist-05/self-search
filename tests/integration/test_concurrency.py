"""
End-to-end regression test for overlapping API requests (concurrency defect, Phase 9).

Runs the real FastAPI app under a real uvicorn server (127.0.0.1, in-process
thread) on a SerializedConnection database. Retrieval uses real SQLite FTS
keyword search; evidence packs, EvidenceValidator, LLMService (scripted
offline model) and product cards are all real and share the one connection,
while another thread runs write transactions on the same connection. Responses
under heavy overlap must equal a sequential baseline.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
import uvicorn

from src.agent.controller import AgentController
from src.api.app import app, open_api_connection, set_agent_controller
from src.db.migrations import run_migrations
from src.evidence.evidence_pack import assemble_evidence_pack
from src.evidence.validator import EvidenceValidator
from src.llm.provider import MockLLMProvider
from src.llm.service import LLMService
from src.retrieval.hybrid import HybridRetriever
from src.retrieval.keyword import keyword_search_products
from tests.conftest import PERSONAL_BRANDS, PERSONAL_CATALOG, PERSONAL_CATEGORIES

QUERIES = ["wireless", "backpack", "keyboard", "speaker", "headphones", "shoes", "earbuds"]
LLM_ANSWER = json.dumps({
    "summary": "Here are some options.",
    "recommendations": [{"product_id": 1, "why_it_matches": [
        {"claim": "Reviewers say it is light and comfortable", "evidence_ids": ["R1"]}]}],
    "insufficient_evidence": False,
})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seed(conn) -> None:
    run_migrations(conn)
    conn.execute("INSERT INTO data_sources (id, name) VALUES (1, 'test')")
    conn.executemany("INSERT INTO categories (id, name) VALUES (?, ?)", PERSONAL_CATEGORIES.items())
    conn.executemany("INSERT INTO brands (id, name) VALUES (?, ?)", PERSONAL_BRANDS.items())
    conn.executemany(
        "INSERT INTO products (id, source_id, external_id, title, description, brand_id, category_id, price, avg_rating) "
        "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)",
        [(pid, f"P{pid:03d}", t, f"{t} wireless light comfortable", b, c, p, r)
         for pid, t, b, c, p, r in PERSONAL_CATALOG],
    )
    conn.executemany(
        "INSERT INTO reviews (product_id, source_id, external_id, rating, title, text, helpful_votes) "
        "VALUES (?, 1, ?, ?, 'r', ?, ?)",
        [(pid, f"R{pid}{k}", 5 - k, f"Product {pid} review {k}: light and comfortable", k)
         for pid, *_ in PERSONAL_CATALOG for k in range(3)],
    )
    conn.commit()


@pytest.fixture
def live_api(tmp_path: Path):
    conn = open_api_connection(tmp_path / "concurrency.db")
    _seed(conn)

    retriever = MagicMock(spec=HybridRetriever)

    def retrieve(**kw):
        return keyword_search_products(conn, kw["query"], limit=kw["limit"], filters=kw.get("filters"))

    def retrieve_with_evidence(**kw):
        products = retrieve(**kw)
        pack = assemble_evidence_pack(conn, [p["id"] for p in products], query=kw["query"],
                                      max_reviews_per_product=kw["max_reviews_per_product"],
                                      token_budget=kw["token_budget"])
        return products, pack

    retriever.retrieve.side_effect = retrieve
    retriever.retrieve_with_evidence.side_effect = retrieve_with_evidence

    controller = AgentController(
        conn=conn, llm_provider=MockLLMProvider(available=False), retriever=retriever,
        llm_service=LLMService(MockLLMProvider(default_response=LLM_ANSWER)),
        validator=EvidenceValidator(conn=conn),
    )
    set_agent_controller(controller)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    try:
        yield f"http://127.0.0.1:{port}", conn
    finally:
        server.should_exit = True
        thread.join(10)
        set_agent_controller(None)
        conn.close()


def _essence(data: dict) -> tuple:
    """Everything in a chat response that must not depend on concurrency."""
    return (
        data["intent"],
        data["result_product_ids"],
        [(p["id"], p["title"], p["price"], p["local_review_count"]) for p in data["products"]],
        [(e["evidence_id"], e["review_id"], e["text"]) for e in data["evidence"]],
        data["support_report"]["total_claims"],
        data["support_report"]["supported_claims"],
        [r["status"] for r in data["support_report"]["claim_results"]],
        data["generation"]["mode"],
    )


def test_overlapping_requests_return_the_same_results_as_sequential(live_api) -> None:
    base, conn = live_api

    def chat(query: str) -> dict:
        resp = requests.post(f"{base}/chat", json={"message": query}, timeout=60)
        assert resp.status_code == 200, resp.text
        return resp.json()

    baseline = {q: _essence(chat(q)) for q in QUERIES}
    assert any(b[1] for b in baseline.values()), "baseline should retrieve products"
    session_id = chat("backpack")["session_id"]

    jobs = [q for _ in range(12) for q in QUERIES]  # 84 chats
    mismatches: list = []
    errors: list = []
    stop = threading.Event()

    def churn() -> None:
        # Write transactions on the API's own connection (a table outside the catalogue),
        # interleaved with the requests' reads.
        n = 0
        while not stop.is_set():
            n += 1
            with conn:
                conn.execute("INSERT INTO query_logs (query) VALUES (?)", (f"churn {n}",))
            with conn:
                conn.execute("DELETE FROM query_logs WHERE query = ?", (f"churn {n}",))

    def probe() -> None:
        while not stop.is_set():
            h = requests.get(f"{base}/health", timeout=30).json()
            d = requests.get(f"{base}/products/3", timeout=30).json()
            sess = requests.get(f"{base}/sessions/{session_id}", timeout=30)
            if h["database"] != "connected" or d["product"]["title"] != "Hiking Backpack" or sess.status_code != 200:
                mismatches.append(("probe", h, d["product"]["title"], sess.status_code))

    def run(query) -> None:
        try:
            got = _essence(chat(query))
            if got != baseline[query]:
                mismatches.append((query, got, baseline[query]))
        except Exception as exc:  # collect, don't hide
            errors.append((query, repr(exc)))

    side = [threading.Thread(target=churn), threading.Thread(target=probe)]
    for t in side:
        t.start()
    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(run, jobs))
    finally:
        stop.set()
        for t in side:
            t.join(30)

    assert errors == []
    assert mismatches == []
    assert conn.execute("SELECT COUNT(*) FROM query_logs WHERE query LIKE 'churn %'").fetchone()[0] == 0
