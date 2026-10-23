# Phase 6 Report: Evidence Validation

**Date:** 2026-10-17  
**Status:** ✅ Complete — all 159 tests passing (1 integration test safely skipped when Ollama is offline)

---

## 1. What Was Built

Phase 6 implements the complete **Evidence Validation Layer** for ShelfSense, fulfilling all requirements of Section 3 (F7), Section 10, and Section 16 of `docs/master-spec.md`:

1. **Citation Integrity Verification (`src/evidence/validator.py`)**:
   - Validates that every citation ID cited in an answer exists in the supplied `EvidencePack` (`FABRICATED_CITATION` detection).
   - Verifies that review citations map to the candidate product being recommended (`CROSS_PRODUCT_MISMATCH` detection).
   - Catches uncited qualitative assertions made without supporting evidence IDs.

2. **Typed Numeric Extraction and Recomputation (`src/evidence/numeric.py`)**:
   - `NumericExtractor` extracts prices, ratings, review counts, and specifications using boundary-enforced regular expressions, avoiding accidental capture of unrelated numbers (e.g. "top 3 products", "version 2").
   - `NumericVerifier` queries SQLite database records (`products` table) directly and audits model assertions against ground truth.
   - **Documented Tolerances**:
     - `price_tolerance` ($0.05 default): Accommodates floating-point conversion artifacts while catching price hallucinations.
     - `rating_tolerance` (0.15 stars default): Permits human-like language model rounding (e.g., 4.43 rounded to 4.4 or 4.5 stars) while catching fabricated ratings.
     - `count_relative_tolerance` (0.20 / 20% relative tolerance): Allows natural rounding for review volumes while identifying order-of-magnitude hallucinations.
   - **Missing Catalog Data Handling (`UNVERIFIABLE`)**:
     - Whenever a database attribute is `NULL` or absent in `attributes_json`, the system returns `UNVERIFIABLE` with `is_match=True`, strictly preventing false assertions against incomplete catalog data.

3. **Grounding Signals (`compute_lexical_overlap`, `compute_embedding_similarity`)**:
   - Computes Jaccard content-token overlap and cosine embedding similarity via `get_local_or_remote_embedding()`, which functions 100% offline via deterministic feature hashing.
   - Treats similarity metrics as heuristic supporting signals (not absolute proof of factual truth) to catch topical drift or irrelevant snippets.

4. **Operating Modes (`flag` vs `filter`)**:
   - **`flag` mode (default)**: Annotates unsupported claims with a `[UNVERIFIED] ` prefix in the returned `StructuredAnswer`, preserving supported claims completely unmodified.
   - **`filter` mode**: Prunes unsupported claims from `why_it_matches` and `watch_outs` without changing supported claims. If no verifiable claims remain across all recommendations, safely returns `StructuredAnswer(insufficient_evidence=True)`.

5. **Auditable `SupportReport` Data Structure (`src/evidence/report.py`)**:
   - Synthesizes `total_claims`, `supported_claims`, `unsupported_claims`, `fabricated_citations`, `cross_product_mismatches`, `numeric_mismatches`, `unverifiable_claims`, `support_rate`, and `is_valid`.
   - Answers are valid only if `support_rate >= 0.70` and zero fabricated IDs, cross-product errors, or numeric mismatches exist.

---

## 2. Files Created and Modified

### Source Modules (`src/evidence/`)
| File | Changes / Purpose |
|---|---|
| `src/evidence/report.py` | `ValidationStatus`, `NumericType`, `NumericCheckResult`, `ClaimValidationResult`, and `SupportReport` |
| `src/evidence/numeric.py` | `NumericExtractor` (typed regexes) and `NumericVerifier` (SQLite ground-truth verification and tolerances) |
| `src/evidence/validator.py` | `EvidenceValidator`, citation checks, similarity grounding, `flag`/`filter` modes, and answer synthesis |
| `src/evidence/__init__.py` | Public API exports for Phase 6 |

### Test Suites & Fixtures (`tests/`)
| File | Tests / Purpose | Status |
|---|---|---|
| `tests/fixtures/evidence_validation_cases.json` | 15 curated benchmark evaluation cases across all failure modes | ✅ Curated Fixture |
| `tests/unit/test_evidence_validator.py` | 22 unit tests for extraction, verifier, tolerances, citation integrity, and modes | ✅ 22 passed (offline) |
| `tests/integration/test_evidence_validation_pipeline.py` | 3 integration tests connecting SQLite catalog, retrieval evidence, `LLMService`, and `EvidenceValidator` | ✅ 3 passed (offline) |

### Scripts & Documentation
| File | Purpose |
|---|---|
| `scripts/evaluate_evidence_validation.py` | Benchmark evaluation script reporting Precision, Recall, FPR, Accuracy, and Limitations |
| `docs/evidence-validation.md` | Architecture, citation checks, numeric tolerances, operating modes, and limitations |
| `docs/phase-reports/phase-6-report.md` | Phase 6 completion report and exit checklist |

---

## 3. Test Suite Results

Full pytest execution (`python -m pytest`):

```
================ 159 passed, 1 skipped, 33 warnings in 23.15s =================
```

### Exact Test Counts
- **Total Tests:** 160
- **Passed:** 159
- **Skipped:** 1 (`tests/integration/test_llm_ollama.py`, skipped offline as designed)
- **Failed:** 0

### Test Breakdown by Category
- **Phase 6 Unit Tests (`tests/unit/test_evidence_validator.py`, 22 passed)**:
  - `TestNumericExtractor` (5 tests): Price, rating, count, spec extraction; unrelated numbers ignored.
  - `TestNumericVerifier` (7 tests): Exact match, price tolerance, rating tolerance, count tolerance, missing DB values (`UNVERIFIABLE`), spec verification, missing product handling.
  - `TestEvidenceValidatorCitations` (4 tests): Valid supported claim, fabricated citation ID, cross-product citation mismatch, uncited claim.
  - `TestFlagVsFilterMode` (3 tests): Flag mode annotation, filter mode pruning, filter mode all-unsupported fallback to `insufficient_evidence: True`.
  - `TestSupportReport` (3 tests): Report metrics, text summary, lexical/semantic overlap helpers.
- **Phase 6 Integration Tests (`tests/integration/test_evidence_validation_pipeline.py`, 3 passed)**:
  - End-to-end faithful answer validation via `MockLLMProvider` and SQLite catalog.
  - End-to-end `flag` mode detection of fabricated citations and incorrect prices.
  - End-to-end `filter` mode pruning and supported claim preservation.
- **Preexisting Unit Tests (127 passed)**: `test_llm.py` (12), `test_retrieval.py` (17), `test_validation.py` (39), `test_ingestion.py` (18), `test_config.py` (15), `test_deduplication.py` (11), `test_schema.py` (10), `test_embeddings.py` (3), `test_vector_store.py` (2).
- **Preexisting Integration Tests (7 passed, 1 skipped)**: `test_retrieval_pipeline.py` (6), `test_ingestion_pipeline.py` (1), `test_llm_ollama.py` (1 skipped).

---

## 4. Benchmark Evaluation Results (`scripts/evaluate_evidence_validation.py`)

Execution on `tests/fixtures/evidence_validation_cases.json`:

```
================================================================================
 EVALUATION METRICS SUMMARY
================================================================================
Total Test Cases (Sample Size N): 15
True Positives (TP - Hallucinations Caught):   7
True Negatives (TN - Valid Claims Supported):   8
False Positives (FP - Valid Claims Rejected):   0
False Negatives (FN - Hallucinations Missed):   0
Precision:            1.0000 (100.0%)
Recall:               1.0000 (100.0%)
False-Positive Rate:  0.0000 (0.0%)
Overall Accuracy:     1.0000 (100.0%)

Breakdown by Category:
  - supported                     : 6/6 correct (100.0%)
  - fabricated_citation           : 1/1 correct (100.0%)
  - cross_product_mismatch        : 1/1 correct (100.0%)
  - numeric_price_mismatch        : 1/1 correct (100.0%)
  - numeric_rating_mismatch       : 1/1 correct (100.0%)
  - numeric_count_mismatch        : 1/1 correct (100.0%)
  - unverifiable_spec             : 1/1 correct (100.0%)
  - low_similarity_hallucination  : 2/2 correct (100.0%)
  - unverifiable_db_null          : 1/1 correct (100.0%)
================================================================================
```

---

## 5. Limitations & Scientific Disclaimer (Requirement 10)

Per Phase 6 requirements and Section 0.2:

1. **Curated Benchmark Scope:**
   These metrics reflect evaluation on a targeted, curated offline fixture dataset (N=15). **100% precision and recall on this suite must NOT be claimed as general factual accuracy or zero hallucination across unrestricted real-world e-commerce queries.**
2. **Deterministic vs Heuristic Boundary:**
   - **Deterministic verification** (exact citation ID lookup, candidate product alignment, SQLite price/rating recomputation) provides rigorous mathematical guarantees within documented tolerances.
   - **Heuristic verification** (lexical Jaccard overlap and embedding cosine similarity) serves as a supporting plausibility signal. While it reliably flags severe topical drift (e.g. citing a yoga mat review for a backpack claim), it cannot detect subtle factual contradictions, logical negations, or nuanced aspect inversions.
3. **Database Completeness:**
   Verification is inherently bounded by SQLite database coverage. Missing attributes produce `UNVERIFIABLE` to avoid penalizing incomplete records, meaning unverified claims remain unproven.

---

## 6. Exit Criteria Checklist (master-spec Section 16)

- [x] **Claim-to-evidence checker**: Validates citation IDs exist in the `EvidencePack` and match candidate products.
- [x] **Grounding similarity**: Lexical and embedding similarity checks implemented with documented heuristic limitations.
- [x] **Numeric recomputation**: Prices, ratings, review counts, and specs verified against SQLite ground truth.
- [x] **Tolerances documented and tested**: Configurable tolerances for prices ($0.05), ratings (0.15), and review counts (20%).
- [x] **Missing DB values**: Missing columns/attributes produce `UNVERIFIABLE`, not a false assertion.
- [x] **Operating modes**: `flag` mode annotates unverified claims with `[UNVERIFIED] `; `filter` mode removes unsupported claims without modifying supported claims.
- [x] **Support report**: Detailed `SupportReport` generated per answer with metrics and readable format.
- [x] **Curated evaluation set & script**: 15 test cases evaluated with Precision, Recall, FPR, Accuracy, and Limitations reported.
- [x] **Full test suite passing**: 159 tests passing, 1 skipped offline integration test, 0 failures.
- [x] **Documentation**: `docs/evidence-validation.md` created; `docs/phase-reports/phase-6-report.md` created.
