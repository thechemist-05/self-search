# Phase 4 Report: Retrieval (RAG Versions 1 to 3)

**Date:** 2026-10-17
**Status:** ✅ Complete — all 122 tests passing

---

## 1. What was built

Phase 4 implements the complete hybrid retrieval and review evidence packaging pipeline for ShelfSense, fulfilling all requirements of Section 9 and Section 16 of `docs/master-spec.md`:

1. **Semantic Retrieval (FAISS)**:
   - Dense vector candidate lookup over isolated product and review indexes (`products.index` and `reviews.index`).
   - SQLite entity hydration mapping internal integer IDs to full candidate records.
   - Offline-safe deterministic embedding fallback ensuring zero network dependency when Ollama is offline.
2. **SQLite FTS5 Keyword Retrieval (BM25)**:
   - BM25 candidate lookup over `products_fts` virtual table.
   - Query sanitisation stripping FTS5 syntax control characters and generating safe `OR` disjunctions.
3. **Metadata Filtering**:
   - `MetadataFilter` dataclass supporting `min_price`, `max_price`, `min_rating`, `category`, and `brand`.
   - Dual execution: compiles to SQL `WHERE` clauses for keyword search and runs in-memory validation for semantic search candidates.
4. **Reciprocal Rank Fusion (RRF)**:
   - Standard RRF implementation combining arbitrary candidate lists with smoothing parameter $k = 60$.
   - Preserves source attribution and individual rank provenance (`source_ranks`).
5. **Optional Cross-Encoder Reranking**:
   - `CrossEncoderReranker` class targeting `cross-encoder/ms-marco-MiniLM-L-6-v2`.
   - Configurable via `SHELFSENSE_ENABLE_CROSS_ENCODER`, falling back gracefully to hybrid fusion in offline environments.
6. **Review Evidence Retrieval & Token Budgeting**:
   - `assemble_evidence_pack` pulling relevant reviews for top candidate products.
   - Consistent citation IDs (`[R1]`, `[R2]`, `[R3]`, etc.) referencing actual review records.
   - Strict token budget enforcement (default: 1,500 tokens).
7. **Interactive CLI & Evaluation Benchmark**:
   - `scripts/run_retrieval.py`: CLI tool supporting all 4 retrieval modes (`keyword`, `semantic`, `hybrid`, `reranked_hybrid`), filters, and evidence pack display.
   - `eval/queries.jsonl`: Curated benchmark of 20 multi-category product queries with ground truth.
   - `scripts/evaluate_retrieval.py`: Benchmark runner computing Recall@1, Recall@3, Recall@5, and MRR.

---

## 2. Files Created and Modified

### New Modules (`src/`)
| File | Purpose |
|---|---|
| `src/retrieval/filters.py` | MetadataFilter dataclass, SQL clause generation, in-memory matching |
| `src/retrieval/keyword.py` | FTS5 BM25 search and query sanitisation |
| `src/retrieval/fusion.py` | Reciprocal Rank Fusion (RRF) algorithm |
| `src/retrieval/rerank.py` | CrossEncoder reranker with offline fallback |
| `src/retrieval/semantic.py` | Semantic search with SQLite candidate hydration |
| `src/retrieval/hybrid.py` | HybridRetriever class coordinating all retrieval stages |
| `src/evidence/evidence_pack.py` | EvidenceItem, EvidencePack, and review evidence assembler |
| `src/evaluation/retrieval_metrics.py` | Recall@K, Precision@K, and MRR computation |

### Updated Modules
| File | Changes Made |
|---|---|
| `src/embeddings/embeddings.py` | Added fast Ollama daemon probe and deterministic offline feature-hashing fallback |
| `src/vector_store/vector_store.py` | Added `get_product_store()` and `get_review_store()` to prevent ID collisions |
| `src/retrieval/retrieval.py` | Exposed `retrieve_products` and `retrieve_with_evidence` while preserving legacy `retrieve()` |
| `src/retrieval/__init__.py` | Exported all Phase 4 classes and functions |
| `src/evidence/__init__.py` | Exported evidence pack classes |
| `scripts/generate_embeddings.py` | Populates separate product and review indexes from `data/processed/shelfsense.db` |
| `docs/rag-design.md` | Comprehensive architectural documentation of Phase 4 |

### Scripts & Evaluation
| File | Purpose |
|---|---|
| `scripts/run_retrieval.py` | CLI multi-mode search and evidence renderer |
| `scripts/evaluate_retrieval.py` | Evaluation runner computing Recall@K and MRR |
| `eval/queries.jsonl` | 20 labelled evaluation queries with expected product IDs |
| `eval/results/retrieval_evaluation.json` | Persisted retrieval benchmark metrics |

### Test Suites (`tests/`)
| File | Tests |
|---|---|
| `tests/unit/test_retrieval.py` | 17 unit tests covering filters, FTS5 sanitisation, RRF, evidence packs, reranking, and metrics |
| `tests/integration/test_retrieval_pipeline.py` | 6 integration tests verifying end-to-end retrieval, filtering, evidence packs, and ID consistency |

---

## 3. Test Suite Results

Full pytest execution (`python -m pytest`):

```
====================== 122 passed, 33 warnings in 17.74s ======================
```

### Test Breakdown by Category
- **Mocked / Unit Tests (115 tests)**:
  - `tests/unit/test_retrieval.py` (17 tests): RRF, FTS5 sanitisation, MetadataFilter, EvidencePack token budgeting, mock reranker, and evaluation metrics.
  - `tests/unit/test_embeddings.py` (3 tests): Mocked Ollama HTTP endpoints.
  - `tests/unit/test_vector_store.py` (2 tests): VectorStore indexing, search, and persistence.
  - `tests/unit/test_config.py` (15 tests): Configuration, logging, settings.
  - `tests/unit/test_deduplication.py` (11 tests): Content hashing, duplicate detection.
  - `tests/unit/test_ingestion.py` (18 tests): Fixture ingestion and source registry.
  - `tests/unit/test_schema.py` (10 tests): SQLite schema, foreign keys, triggers.
  - `tests/unit/test_validation.py` (39 tests): Price parsing, text cleaning, product and review validation.
- **Unmocked / Integration Tests (7 tests)**:
  - `tests/integration/test_retrieval_pipeline.py` (6 tests): End-to-end retrieval across modes against real SQLite and FAISS indexes; verification of review citations and ID consistency.
  - `tests/integration/test_ingestion_pipeline.py` (1 test): Full ingestion pipeline validation.

---

## 4. Retrieval Benchmark Results

Evaluated on `eval/queries.jsonl` (20 queries):

| Retrieval Mode | Recall@1 | Recall@3 | Recall@5 | MRR |
|---|---|---|---|---|
| **KEYWORD** | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| **SEMANTIC** | 0.8000 | 1.0000 | 1.0000 | 0.8917 |
| **HYBRID** | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| **RERANKED_HYBRID** | 1.0000 | 1.0000 | 1.0000 | 1.0000 |

---

## 5. Exit Criteria Checklist (master-spec Section 16)

- [x] **Semantic retrieval using FAISS index**: Implemented and verified via `semantic_search_products`.
- [x] **Keyword retrieval using SQLite FTS5**: Implemented with BM25 scoring and query sanitisation.
- [x] **Metadata filtering**: Implemented for price bounds, category, brand, and rating.
- [x] **Reciprocal Rank Fusion (RRF)**: Implemented with rank tracking and configurable $k$.
- [x] **Optional cross-encoder reranker**: Implemented with offline safety and fallback.
- [x] **Review evidence retrieval and token-budgeted evidence packs**: Implemented with stable `[R1]` citation IDs and token caps.
- [x] **Retrieval evaluation on small labelled query set**: Curated `eval/queries.jsonl` and benchmark script executed.
- [x] **Unit and integration tests**: 23 new Phase 4 tests added; full suite of 122 tests passing.
- [x] **CLI Script**: `scripts/run_retrieval.py` executable for all modes with score reporting.
