"""Latency work (2026-10-22): bounded, relevant evidence and one model-time budget per message.

None of this may weaken grounding: the model still sees only products that have
evidence, every model call is bounded, and running out of time gives the safe fallback.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest
import requests

from frontend.api_client import ApiError, ShelfSenseClient
from frontend.config import FrontendConfig
from frontend.view_models import error_message
from src.agent.query_parser import QueryParser
from src.config.settings import get_settings
from src.evidence.evidence_pack import assemble_evidence_pack
from src.llm.deadline import model_time_budget, remaining_seconds
from src.llm.ollama import OllamaProvider, OllamaTimeoutError
from src.llm.provider import LLMResponse, MockLLMProvider
from src.llm.service import LLMService

VALID = json.dumps({"summary": "ok", "recommendations": [], "insufficient_evidence": False})
PRODUCTS = [{"id": 1, "title": "Bag", "price": 10.0}]


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch: pytest.MonkeyPatch):
    for var in ("SHELFSENSE_OLLAMA_TIMEOUT", "SHELFSENSE_QUERY_PARSER_MODE"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _reviews_db(rows: list[tuple[int, str, str, int]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, external_id TEXT)")
    conn.execute("CREATE TABLE reviews (id INTEGER PRIMARY KEY, product_id INTEGER, rating REAL, title TEXT, "
                 "text TEXT, helpful_votes INTEGER, verified_flag INTEGER)")
    conn.executemany("INSERT INTO products VALUES (?, ?)", [(1, "A1"), (2, "A2")])
    conn.executemany("INSERT INTO reviews VALUES (?, ?, 5.0, ?, ?, ?, 1)", rows)
    return conn


class TestEvidenceSelection:
    def test_reviews_mentioning_the_query_come_first(self) -> None:
        conn = _reviews_db([
            (1, 1, "Great", "Lovely colour and fast delivery", 50),
            (2, 1, "Comfy", "Very comfortable for running every morning", 1),
            (3, 1, "Ok", "Fine product overall", 20),
        ])
        pack = assemble_evidence_pack(conn, [1], query="comfortable running shoes", max_reviews_per_product=2)
        assert [i.review_id for i in pack.items] == [2, 1]  # query match first, then helpful votes

    def test_without_a_query_the_order_is_unchanged(self) -> None:
        conn = _reviews_db([(1, 1, "", "first text here", 1), (2, 1, "", "second text here", 9)])
        assert [i.review_id for i in assemble_evidence_pack(conn, [1], query="").items] == [2, 1]

    def test_near_duplicate_reviews_are_skipped(self) -> None:
        same = "These boots are warm and waterproof and I love them a lot every day"
        conn = _reviews_db([(1, 1, "", same, 9), (2, 1, "", same + " really", 8), (3, 1, "", "Too small", 1)])
        pack = assemble_evidence_pack(conn, [1], query="boots", max_reviews_per_product=3)
        assert [i.review_id for i in pack.items] == [1, 3]

    def test_snippets_are_truncated_to_max_words(self) -> None:
        conn = _reviews_db([(1, 1, "", " ".join(f"w{i}" for i in range(100)), 1)])
        item = assemble_evidence_pack(conn, [1], max_words=10).items[0]
        assert item.text == " ".join(f"w{i}" for i in range(10)) + "..."

    def test_evidence_text_is_verbatim_from_the_database(self) -> None:
        conn = _reviews_db([(1, 1, "T", "Exact review words.", 1), (2, 2, "U", "Other product words.", 1)])
        pack = assemble_evidence_pack(conn, [1, 2], query="words")
        assert {(i.product_id, i.text) for i in pack.items} == {(1, "Exact review words."), (2, "Other product words.")}


class TestRetrieveWithEvidence:
    def test_evidence_only_for_the_first_products(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.retrieval import hybrid

        seen = {}
        retriever = hybrid.HybridRetriever.__new__(hybrid.HybridRetriever)
        retriever.conn = None
        monkeypatch.setattr(retriever, "retrieve", lambda **kw: [{"id": i} for i in range(1, 11)], raising=False)
        monkeypatch.setattr(hybrid, "assemble_evidence_pack",
                            lambda **kw: seen.update(kw) or "pack")
        products, pack = retriever.retrieve_with_evidence("q", evidence_products=4, max_words=60)
        assert len(products) == 10 and pack == "pack"
        assert seen["product_ids"] == [1, 2, 3, 4] and seen["max_words"] == 60


class SlowProvider(MockLLMProvider):
    """Records the timeout of each call; optionally sleeps and returns invalid JSON."""

    def __init__(self, sleep: float = 0.0, response: str = VALID) -> None:
        super().__init__(default_response=response)
        self.sleep = sleep
        self.timeouts: list[float] = []

    def generate(self, messages, temperature=0.2, **kwargs) -> LLMResponse:
        self.timeouts.append(kwargs.get("timeout"))
        time.sleep(self.sleep)
        return super().generate(messages, temperature=temperature)


class TestModelTimeBudget:
    def test_each_call_gets_at_most_the_answer_deadline(self) -> None:
        provider = SlowProvider()
        _, info = LLMService(provider, deadline_s=100).generate_answer_with_mode("q", PRODUCTS)
        assert info.mode == "llm" and 99 < provider.timeouts[0] <= 100

    def test_retry_is_skipped_when_too_little_time_remains(self) -> None:
        provider = SlowProvider(sleep=0.3, response="not json")
        svc = LLMService(provider, max_retries=2, deadline_s=30.2)  # < MIN_RETRY_SECONDS left after one call
        _, info = svc.generate_answer_with_mode("q", PRODUCTS)
        assert len(provider.timeouts) == 1
        assert (info.mode, info.reason) == ("fallback", "invalid_output_after_retries")

    def test_retries_still_happen_with_enough_time(self) -> None:
        provider = SlowProvider(response="not json")
        _, info = LLMService(provider, max_retries=2, deadline_s=300).generate_answer_with_mode("q", PRODUCTS)
        assert len(provider.timeouts) == 3 and info.reason == "invalid_output_after_retries"

    def test_request_budget_caps_the_answer(self) -> None:
        provider = SlowProvider()
        with model_time_budget(20):
            LLMService(provider, deadline_s=180).generate_answer_with_mode("q", PRODUCTS)
        assert provider.timeouts[0] <= 20

    def test_exhausted_budget_gives_the_safe_fallback_without_a_call(self) -> None:
        provider = SlowProvider()
        with model_time_budget(0):
            answer, info = LLMService(provider).generate_answer_with_mode("q", PRODUCTS)
        assert provider.timeouts == [] and info.mode == "fallback"
        assert [r.product_id for r in answer.recommendations] == [1]  # real product, no invented text

    def test_parse_and_answer_share_one_budget(self) -> None:
        parse = SlowProvider(sleep=0.5, response=json.dumps({
            "intent": "PRODUCT_SEARCH", "raw_query": "x", "search_text": "bags", "brand": "Acme", "confidence": 0.9}))
        answer = SlowProvider()
        with model_time_budget(10):
            QueryParser(parse, mode="llm_first").parse("bags from Acme")
            LLMService(answer, deadline_s=180).generate_answer_with_mode("bags", PRODUCTS)
        assert parse.timeouts[0] <= QueryParser.LLM_PARSE_TIMEOUT_S
        assert answer.timeouts[0] <= 9.5  # what the parse left over

    def test_outside_a_budget_the_callers_limit_applies(self) -> None:
        assert remaining_seconds(42) == 42

    def test_controller_opens_a_budget_of_the_configured_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.agent import controller as ctl

        monkeypatch.setenv("SHELFSENSE_OLLAMA_TIMEOUT", "77")
        get_settings.cache_clear()
        seen = []
        c = ctl.AgentController.__new__(ctl.AgentController)
        monkeypatch.setattr(c, "_handle_message", lambda *a: seen.append(remaining_seconds(1000)) or "r",
                            raising=False)
        assert c.handle_message("hi") == "r"
        assert 76 < seen[0] <= 77
        assert remaining_seconds(1000) == 1000  # budget closed afterwards


class TestOllamaRequest:
    def _capture(self, monkeypatch: pytest.MonkeyPatch, failure: Exception | None = None) -> dict:
        sent: dict = {}

        class Resp:
            def raise_for_status(self): pass
            def json(self): return {"message": {"content": "{}"}}

        def post(url, json, timeout):
            sent.update(payload=json, timeout=timeout)
            if failure:
                raise failure
            return Resp()

        monkeypatch.setattr(requests, "post", post)
        return sent

    def test_output_cap_keep_alive_and_bounded_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._capture(monkeypatch)
        OllamaProvider(timeout=180).generate([{"role": "user", "content": "hi"}], timeout=37.5)
        assert sent["timeout"] == 37.5
        assert sent["payload"]["options"]["num_predict"] == 700
        assert sent["payload"]["keep_alive"] == "30m"
        assert "timeout" not in sent["payload"]

    def test_a_call_never_waits_longer_than_the_provider_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._capture(monkeypatch)
        OllamaProvider(timeout=60).generate([{"role": "user", "content": "hi"}], timeout=500)
        assert sent["timeout"] == 60

    def test_read_timeout_is_reported_as_a_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._capture(monkeypatch, requests.exceptions.ReadTimeout("read timed out"))
        with pytest.raises(OllamaTimeoutError):
            OllamaProvider().generate([{"role": "user", "content": "hi"}])


class TestUiTimeoutMessage:
    def _client(self, failure: Exception) -> ShelfSenseClient:
        class Session:
            def request(self, *a, **k):
                raise failure
        return ShelfSenseClient(FrontendConfig(timeout_s=120), session=Session())

    def test_read_timeout_is_not_reported_as_unreachable(self) -> None:
        with pytest.raises(ApiError) as exc:
            self._client(requests.ReadTimeout("read timed out")).chat("shoes")
        assert exc.value.kind == "timeout" and "120" in exc.value.detail
        msg = error_message("timeout")
        assert "took too long" in msg and "Self Search is running" in msg and "not reachable" not in msg

    @pytest.mark.parametrize("failure", [requests.ConnectionError("refused"), requests.ConnectTimeout("connect")])
    def test_connection_failures_are_unreachable(self, failure: Exception) -> None:
        with pytest.raises(ApiError) as exc:
            self._client(failure).chat("shoes")
        assert exc.value.kind == "unreachable"


class TestBudgetFitsTheUi:
    def test_default_budget_ends_before_the_ui_gives_up(self) -> None:
        assert get_settings().ollama_timeout < FrontendConfig().timeout_s

    def test_null_claim_lists_mean_none(self) -> None:
        from pydantic import ValidationError

        from src.llm.schemas import StructuredAnswer

        a = StructuredAnswer.model_validate(
            {"summary": "s", "recommendations": [{"product_id": 1, "why_it_matches": None, "watch_outs": None}]})
        assert a.recommendations[0].watch_outs == [] and a.recommendations[0].why_it_matches == []
        with pytest.raises(ValidationError):
            StructuredAnswer.model_validate({"summary": "s", "recommendations": [{"product_id": 1, "watch_outs": "x"}]})
