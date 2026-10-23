# Phase 5 Report: Local LLM Layer

**Date:** 2026-10-17
**Status:** ✅ Complete — all 134 tests passing (1 integration test skipped when Ollama is offline)

---

## 1. What Was Built

Phase 5 implements the complete local language generation layer for ShelfSense, fulfilling all requirements of Section 10 and Section 16 of `docs/master-spec.md`:

1. **`LLMProvider` Interface & Implementations**:
   - `LLMProvider` abstract base class defining `generate()` and `is_available()`.
   - `OllamaProvider`: local open-weight inference via Ollama HTTP API (`/api/chat`) with latency tracking and typed exception hierarchy (`OllamaConnectionError`, `OllamaTimeoutError`).
   - `MockLLMProvider`: fast, deterministic mock provider for reproducible offline unit testing.
2. **Pydantic Response Schemas**:
   - `StructuredAnswer`, `ProductRecommendation`, and `EvidenceClaim` matching Section 10 of `master-spec.md`.
   - Strict validation of factual claims, citations (`evidence_ids`), and insufficient evidence flags.
3. **Structured Prompt Construction**:
   - `build_chat_messages` formatting system instructions, user inquiry, active constraints, candidate product metadata, and token-budgeted review evidence packs into citation-grounded prompts.
4. **Retry Loop & Safe Fallback**:
   - `LLMService` parses model outputs (including markdown code blocks), validates against `StructuredAnswer`, and executes up to 2 self-correction retries on malformed outputs.
   - `build_safe_fallback()` generates a deterministic, grounded answer directly from retrieved records and review evidence without any hallucinated data if retries are exhausted or Ollama is offline.
5. **Insufficient Evidence Handling**:
   - Explicitly returns `insufficient_evidence: true` and describes missing data when candidate products or review evidence are inadequate, never inventing recommendations or citations.

---

## 2. Files Created and Modified

### Source Modules (`src/llm/`)
| File | Purpose |
|---|---|
| `src/llm/provider.py` | `LLMProvider` interface, `LLMResponse`, and `MockLLMProvider` |
| `src/llm/ollama.py` | `OllamaProvider` connecting to local Ollama daemon with latency logging |
| `src/llm/schemas.py` | `EvidenceClaim`, `ProductRecommendation`, and `StructuredAnswer` schemas |
| `src/llm/prompts.py` | Structured system and user prompt builder |
| `src/llm/service.py` | High-level `LLMService` with schema validation, retry loop, and safe fallback |
| `src/llm/__init__.py` | Public API exports for Phase 5 |

### Documentation
| File | Purpose |
|---|---|
| `docs/llm-design.md` | Architecture, schemas, prompt templates, retry strategy, and Ollama setup guide |
| `docs/phase-reports/phase-5-report.md` | Phase 5 completion report and exit checklist |

### Test Suites (`tests/`)
| File | Tests | Status |
|---|---|---|
| `tests/unit/test_llm.py` | 12 unit tests covering schemas, prompt formatting, JSON extraction, retries, and fallback | ✅ 12 passed (offline) |
| `tests/integration/test_llm_ollama.py` | 1 integration test with real Ollama model | ⏭️ Skipped when Ollama offline |

---

## 3. Test Suite Results

Full pytest execution (`python -m pytest`):

```
================ 134 passed, 1 skipped, 33 warnings in 20.31s =================
```

### Test Breakdown by Category
- **Unit / Mocked Tests (127 passing)**:
  - `tests/unit/test_llm.py` (12 tests): Schemas, prompt construction, JSON extraction, retry loop, and fallback generation with `MockLLMProvider`.
  - `tests/unit/test_retrieval.py` (17 tests): Phase 4 hybrid retrieval, FTS5, RRF, evidence packs, and metrics.
  - Preexisting unit tests (98 tests): `test_validation.py` (39), `test_ingestion.py` (18), `test_config.py` (15), `test_deduplication.py` (11), `test_schema.py` (10), `test_embeddings.py` (3), `test_vector_store.py` (2).
- **Integration Tests (7 passing, 1 skipped)**:
  - `tests/integration/test_retrieval_pipeline.py` (6 tests): End-to-end multi-mode retrieval and evidence packaging.
  - `tests/integration/test_ingestion_pipeline.py` (1 test): Complete ingestion pipeline.
  - `tests/integration/test_llm_ollama.py` (1 test): Real Ollama model integration test (skipped when daemon is not running on localhost:11434).

---

## 4. Exit Criteria Checklist (master-spec Section 16)

- [x] **`LLMProvider` interface**: Abstract base class and `OllamaProvider` implementation.
- [x] **Prompt templates**: Structured prompts injecting query, constraints, products, and evidence pack.
- [x] **JSON schema validation**: Validated against `StructuredAnswer` Pydantic model.
- [x] **Limited retries**: Automated self-correction retry loop on malformed outputs.
- [x] **Safe fallback**: Deterministic, hallucination-free fallback using only retrieved data.
- [x] **Insufficient-evidence handling**: Sets `insufficient_evidence: true` when evidence is missing.
- [x] **Latency logging**: Latency tracked in milliseconds via `structlog`.
- [x] **Unit & Integration tests**: 12 offline unit tests passing; 1 live Ollama integration test safely skipped offline.
- [x] **Documentation**: `docs/llm-design.md` updated with setup guide and hardware assumptions.
