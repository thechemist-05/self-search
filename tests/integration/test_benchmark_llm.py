"""scripts/benchmark_llm.py: real pipeline, mock model, temporary database copy only."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import benchmark_llm as bench
from scripts import generate_embeddings as gen
from src.config.settings import PROJECT_ROOT, get_settings
from src.embeddings import embeddings as emb
from src.llm.ollama import OllamaConnectionError
from src.llm.provider import MockLLMProvider
from src.vector_store import manifest as mf

QUERIES = bench.load_queries(PROJECT_ROOT / "eval" / "queries.jsonl")[:3]


@pytest.fixture
def served(catalogue_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(emb, "check_ollama_available", lambda: False)
    for var in ("VECTOR_STORE_PATH", "PRODUCT_INDEX_PATH", "REVIEW_INDEX_PATH", "SHELFSENSE_QUERY_PARSER_MODE"):
        monkeypatch.delenv(var, raising=False)
    index_dir = tmp_path / "vs"
    gen.rebuild_indexes(catalogue_db, index_dir)
    monkeypatch.setenv("SHELFSENSE_VECTOR_STORE_DIR", str(index_dir))
    get_settings.cache_clear()
    yield catalogue_db, index_dir
    get_settings.cache_clear()


def run(served, provider: MockLLMProvider) -> dict:
    db, index_dir = served
    return bench.benchmark_model("mock", QUERIES, db, index_dir, lambda _m: provider, log=lambda _s: None)


def test_unreachable_model_is_measured_as_fallback(served) -> None:
    def down(_messages):
        raise OllamaConnectionError("connection refused")

    result = run(served, MockLLMProvider(handler=down))
    s = result["summary"]
    assert (s["queries"], s["answered"], s["llm_success_rate"], s["fallback_rate"]) == (3, 3, 0.0, 1.0)
    assert s["fallback_reasons"] == {"provider_unavailable": 3}
    assert s["model_call_errors"] == {"OllamaConnectionError": 3}  # warm-up excluded
    # Product-first: a search never calls the model; its optional summary calls it once.
    kinds = [r["kind"] for r in result["queries"]]
    assert kinds == ["search", "summary"] * 3
    assert s["searches_calling_the_model"] == 0
    assert all(len(r["model_calls"]) == (r["kind"] == "summary") for r in result["queries"])
    assert s["search_latency_seconds"]["max"] is not None and s["summary_latency_seconds"]["max"] is not None
    assert s["relevant_retrieved_rate"] == 1.0


def test_searches_only_never_call_the_model(served) -> None:
    db, index_dir = served
    provider = MockLLMProvider(default_response="not json")
    result = bench.benchmark_model("mock", QUERIES, db, index_dir, lambda _m: provider, log=lambda _s: None,
                                   summaries=False)
    assert provider.call_count == 1  # the warm-up only
    assert [r["kind"] for r in result["queries"]] == ["search"] * 3
    assert result["summary"]["answered"] == 0


def test_invalid_json_counts_against_output_validity(served) -> None:
    s = run(served, MockLLMProvider(default_response="not json"))["summary"]
    assert s["fallback_reasons"] == {"invalid_output_after_retries": 3}
    assert s["output_validity"]["invalid_after_retries"] == 3
    assert s["output_validity"]["first_attempt_valid_rate"] == 0.0
    assert s["evidence_compliance_llm_answers"]["answers"] == 0


def test_source_database_and_indexes_are_not_modified(served) -> None:
    db, index_dir = served
    before = (mf.file_sha256(db), {p.name: mf.file_sha256(p) for p in index_dir.iterdir()})
    run(served, MockLLMProvider(default_response="not json"))
    assert (mf.file_sha256(db), {p.name: mf.file_sha256(p) for p in index_dir.iterdir()}) == before


def test_summary_metrics() -> None:
    ok = {"total_claims": 4, "supported_claims": 4, "unsupported_claims": 0, "fabricated_citations": 0,
          "cross_product_mismatches": 0, "numeric_mismatches": 0, "support_rate": 1.0, "is_valid": True}
    weak = {**ok, "supported_claims": 2, "unsupported_claims": 1, "numeric_mismatches": 1,
            "support_rate": 0.5, "is_valid": False}
    recs = [
        {"mode": "llm", "reason": None, "attempts": 1, "seconds": 10.0, "evidence": ok,
         "model_calls": [{"ok": True}], "relevant_retrieved": True},
        {"mode": "llm", "reason": None, "attempts": 2, "seconds": 30.0, "evidence": weak,
         "model_calls": [{"ok": True}, {"ok": True}], "relevant_retrieved": False},
        {"mode": "fallback", "reason": "provider_error", "attempts": 1, "seconds": 180.0, "evidence": {},
         "model_calls": [{"ok": False, "error": "OllamaError"}], "relevant_retrieved": True},
        {"mode": None, "reason": None, "attempts": None, "seconds": 0.1, "evidence": {},
         "model_calls": [], "relevant_retrieved": None},
    ]
    s = bench.summarize(recs)
    assert (s["answered"], s["llm_success_rate"], s["fallback_rate"]) == (3, 0.667, 0.333)
    assert s["output_validity"] == {"first_attempt_valid_rate": 0.333, "answers_needing_retry": 1,
                                    "invalid_after_retries": 0}
    ev = s["evidence_compliance_llm_answers"]
    assert (ev["all_claims_supported_rate"], ev["mean_support_rate"], ev["numeric_mismatches"]) == (0.5, 0.75, 1)
    assert s["latency_seconds"] == {"median": 30.0, "mean": 73.3, "max": 180.0}
    assert s["model_call_errors"] == {"OllamaError": 1}
    assert s["relevant_retrieved_rate"] == 0.667
