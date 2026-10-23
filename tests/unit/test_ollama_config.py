"""Local Ollama wiring: model name, timeout, safe fallback, and embeddings kept on the index's embedder."""

from __future__ import annotations

import pytest
import requests

from src.config.settings import Settings, get_settings
from src.embeddings import embeddings as emb
from src.llm.ollama import OllamaError, OllamaProvider
from src.llm.service import LLMService

PRODUCTS = [{"id": 6, "title": "Trail Shoes", "brand_name": "TrailMaster", "price": 89.99, "currency": "USD",
             "avg_rating": 4.5}]


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch: pytest.MonkeyPatch):
    for var in ("SHELFSENSE_OLLAMA_MODEL", "SHELFSENSE_OLLAMA_TIMEOUT", "SHELFSENSE_OLLAMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults_match_the_installed_model_and_local_endpoint() -> None:
    s = Settings(_env_file=None)
    assert s.ollama_model == "llama3.2:3b"
    assert s.ollama_base_url == "http://localhost:11434"
    assert s.ollama_timeout == 110  # below the UI's 120 s request timeout


def test_provider_uses_configured_model_url_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELFSENSE_OLLAMA_MODEL", "custom:1b")
    monkeypatch.setenv("SHELFSENSE_OLLAMA_TIMEOUT", "45")
    get_settings.cache_clear()
    p = OllamaProvider()
    assert (p.model, p.timeout, p.base_url) == ("custom:1b", 45, "http://localhost:11434")
    assert OllamaProvider(timeout=5).timeout == 5  # explicit argument still wins


def test_request_sends_the_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = {}

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"message": {"content": "{}"}}

    def post(url, json, timeout):
        sent.update(url=url, model=json["model"], timeout=timeout)
        return Resp()

    monkeypatch.setattr(requests, "post", post)
    OllamaProvider().generate([{"role": "user", "content": "hi"}])
    assert sent == {"url": "http://localhost:11434/api/chat", "model": "llama3.2:3b", "timeout": 110}


@pytest.mark.parametrize("failure", [
    requests.exceptions.HTTPError("404 Client Error: model 'missing:1b' not found"),
    requests.exceptions.ReadTimeout("Read timed out. (read timeout=180)"),
    requests.exceptions.ConnectionError("refused"),
])
def test_missing_model_timeout_or_offline_daemon_use_the_safe_fallback(monkeypatch, failure) -> None:
    def post(*a, **k):
        raise failure

    monkeypatch.setattr(requests, "post", post)
    answer, info = LLMService(provider=OllamaProvider(model="missing:1b")).generate_answer_with_mode("shoes", PRODUCTS)
    assert info.mode == "fallback" and info.reason in ("provider_error", "provider_unavailable")
    assert [r.product_id for r in answer.recommendations] == [6]  # deterministic, from retrieved data


def test_embeddings_stay_offline_even_when_ollama_is_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emb, "check_ollama_available", lambda: True)
    monkeypatch.setattr(emb, "_EMBEDDING_BACKEND", "offline")

    def no_ollama(_text):
        raise AssertionError("Ollama embeddings must not be called unless explicitly selected")

    monkeypatch.setattr(emb, "get_embedding", no_ollama)
    vec = emb.get_local_or_remote_embedding("trail running shoes")
    assert vec == emb.deterministic_embedding("trail running shoes") and len(vec) == 64


def test_ollama_embeddings_only_on_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emb, "check_ollama_available", lambda: True)
    monkeypatch.setattr(emb, "_EMBEDDING_BACKEND", "ollama")
    monkeypatch.setattr(emb, "get_embedding", lambda text: [0.5] * 768)
    assert len(emb.get_local_or_remote_embedding("x")) == 768
    monkeypatch.setattr(emb, "get_embedding", lambda text: (_ for _ in ()).throw(OllamaError("no model")))
    assert len(emb.get_local_or_remote_embedding("x")) == 64  # failure falls back to offline
