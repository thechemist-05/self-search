"""Real pipeline (fixture catalogue, hybrid retrieval, validator): the answer prompt is bounded."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import benchmark_llm as bench
from scripts import generate_embeddings as gen
from src.api.app import open_api_connection
from src.config.settings import get_settings
from src.embeddings import embeddings as emb
from src.llm.provider import MockLLMProvider


@pytest.fixture
def controller_and_provider(catalogue_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(emb, "check_ollama_available", lambda: False)
    for var in ("VECTOR_STORE_PATH", "PRODUCT_INDEX_PATH", "REVIEW_INDEX_PATH", "SHELFSENSE_QUERY_PARSER_MODE"):
        monkeypatch.delenv(var, raising=False)
    index_dir = tmp_path / "vs"
    gen.rebuild_indexes(catalogue_db, index_dir)
    monkeypatch.setenv("SHELFSENSE_VECTOR_STORE_DIR", str(index_dir))
    get_settings.cache_clear()
    conn = open_api_connection(catalogue_db)
    provider = MockLLMProvider(default_response="not json")  # forces the safe fallback after recording
    yield bench.build_controller(conn, provider, index_dir), provider
    conn.close()
    get_settings.cache_clear()


def test_search_is_product_first(controller_and_provider) -> None:
    controller, provider = controller_and_provider
    response = controller.handle_message("wireless")
    assert provider.call_count == 0
    assert response.products and response.generation.mode == "none"


def test_summary_sees_at_most_three_products_and_only_their_evidence(controller_and_provider) -> None:
    controller, provider = controller_and_provider
    search = controller.handle_message("wireless")  # matches many fixture products
    response = controller.summarise_results(search.session_id, search.result_product_ids)
    prompt = provider.history[0][1]["content"]
    prompt_ids = [int(x) for x in re.findall(r"- Product ID: (\d+)", prompt)]
    evidence_ids = {int(x) for x in re.findall(r"\(Product ID: (\d+)", prompt)}
    assert 1 <= len(prompt_ids) <= 3
    assert prompt_ids == search.result_product_ids[: len(prompt_ids)]  # the top results, in order
    assert evidence_ids <= set(prompt_ids)
    assert {e.product_id for e in response.evidence} <= set(prompt_ids)  # the validator saw the same pack
    assert response.result_product_ids == search.result_product_ids  # every result is still shown
    assert len(prompt) < 6000  # was about 13,000 characters on the Fashion catalogue
    assert response.generation.mode == "fallback"
    recommended = [r["product_id"] for r in response.answer["recommendations"]]
    assert recommended and set(recommended) <= set(prompt_ids)
