# Self Search Evidence Validation Architecture (Phase 6)

## 1. Overview and Purpose

Self Search operates under a strict principle defined in `docs/master-spec.md`:
> *The LLM is not the database, not the search engine, and not the source of truth. It is only the language layer. All facts come from retrieved data, and every factual claim shown to the user must be traceable to evidence.*

The **Evidence Validation Layer** (`src/evidence/validator.py`, `src/evidence/numeric.py`, `src/evidence/report.py`) serves as an automated verification gate situated between the local LLM generation layer (Phase 5) and the conversational agent / user interface (Phases 7 & 9). Its mission is to scrutinize every factual claim made in `why_it_matches` and `watch_outs`, intercepting hallucinations, fabricated citations, cross-product confusion, and numeric drift before any unverified assertion reaches the user.

---

## 2. Validation Architecture and Components

```
                StructuredAnswer (from LLM)
                           |
                           v
        +-------------------------------------+
        |         EvidenceValidator           |
        +-------------------------------------+
           |               |               |
           v               v               v
    1. Citation ID    2. Numeric      3. Grounding
       Integrity       Recomputation     Signals
     (pack lookup &   (SQLite ground   (lexical &
      cross-product)    truth check)    embedding)
           \               |               /
            +--------------+--------------+
                           |
                           v
             Validation Diagnosis per Claim
         (SUPPORTED, NUMERIC_MISMATCH, etc.)
                           |
             +-------------+-------------+
             |                           |
             v                           v
        'flag' mode                 'filter' mode
         (default)              (drops unsupported)
    Annotate unverified         Prune invalid claims;
      claims in answer         if all dropped -> safe
                                insufficient_evidence
                           |
                           v
          (Sanitized Answer, SupportReport)
```

### 2.1 Citation Integrity Verification
- **Existence Verification:** Verifies that every evidence ID cited in an `EvidenceClaim` (e.g. `R1`, `R2`) actually exists in the provided `EvidencePack`. Hallucinated identifiers (e.g. `R999_FABRICATED`) are immediately flagged as `FABRICATED_CITATION`.
- **Product-Review Alignment:** Asserts that review citations belong to the candidate product being recommended (`item.product_id == rec.product_id`). Borrowing a glowing review from Product A to praise Product B is classified as `CROSS_PRODUCT_MISMATCH`.
- **Uncited Claims:** If a factual claim provides no citation IDs and is not verified by structured database values, it is classified as `UNSUPPORTED_LOW_SIMILARITY`.

### 2.2 Typed Numeric Extraction and Recomputation
Language models frequently suffer from numeric hallucinations (e.g. inventing lower prices, exaggerating star ratings, or inflating review counts). Self Search recomputes all numeric claims against the SQLite product record:

1. **Extraction Hygiene:** `NumericExtractor` uses targeted regular expressions with mandatory unit or contextual boundaries to distinguish typed metrics without capturing unrelated general numbers (e.g. "version 2", "top 3 products"):
   - **Prices:** Requires `$` or `dollars`/`USD` prefixes/suffixes (`$49.99`, `$1,299.00`, `30 USD`).
   - **Ratings:** Requires explicit star or scale identifiers (`4.5 stars`, `4.8/5`, `4 out of 5`).
   - **Review Counts:** Requires review or rating tokens (`1,250 reviews`, `8,900 customer reviews`).
   - **Specifications:** Requires physical units (`100W`, `20000mAh`, `40L`, `32oz`, `6ft`, `6mm`, `5 lbs`, `10 hours`).

2. **Numeric Tolerances (`NumericVerifier`):**
   - **`price_tolerance` ($0.05 default):** Accommodates floating-point conversion artifacts (e.g. $49.99 vs 49.990001) while detecting actual price discrepancies ($14.99 vs $49.99).
   - **`rating_tolerance` (0.15 stars default):** Allows natural rounding by the language model (e.g., rounding an average rating of 4.43 to 4.4 or 4.5 stars) while flagging hallucinations (e.g. claiming 2.1 stars for a 4.3-rated product).
   - **`count_relative_tolerance` (0.20 / 20% relative tolerance):** Permits human-like approximations for review counts (e.g. "over 1,300 reviews" when catalog records 1,250) while rejecting order-of-magnitude hallucinations (e.g. "55,000 reviews").

3. **Handling Missing Database Values (`UNVERIFIABLE`):**
   - If a product record has `NULL` for price, rating, or review count, or if a specification is absent in `attributes_json`, the verifier returns `status='UNVERIFIABLE'` with `is_match=True`.
   - Adheres strictly to the specification requirement: **Missing catalog data produces `UNVERIFIABLE`, not a false assertion or penalty.**

### 2.3 Grounding Signals (Lexical & Semantic Overlap)
- **Lexical Overlap:** Computed via Jaccard token overlap over content words (excluding standard English stop words).
- **Semantic Overlap:** Computed via cosine similarity of normalized text embeddings using `get_local_or_remote_embedding()`, which functions 100% offline via deterministic feature hashing when Ollama is unavailable.
- **Supporting Signal Philosophy:** High similarity does not prove factual truth, and low similarity does not prove intentional malice. These metrics serve as heuristic supporting signals to flag claims that cite snippets with zero topical relevance.

---

## 3. Operating Modes

| Mode | Default? | Behavior on Supported Claims | Behavior on Unsupported Claims | Fallback When Zero Claims Remain |
| :--- | :---: | :--- | :--- | :--- |
| **`flag`** | **Yes** | Kept completely unmodified | Annotated with `[UNVERIFIED] ` prefix | Recommendations kept with annotations |
| **`filter`** | No | Kept completely unmodified | Dropped entirely from recommendations | Returns `StructuredAnswer(insufficient_evidence=True)` |

In neither mode is the wording or meaning of a supported claim ever altered.

---

## 4. SupportReport Data Structure

Each validation pass generates a comprehensive, auditable `SupportReport`:

```json
{
  "query": "waterproof lightweight backpack",
  "total_claims": 3,
  "supported_claims": 2,
  "unsupported_claims": 1,
  "fabricated_citations": 0,
  "cross_product_mismatches": 0,
  "numeric_mismatches": 1,
  "unverifiable_claims": 0,
  "support_rate": 0.6667,
  "is_valid": false,
  "claim_results": [...]
}
```

An answer is marked `is_valid: true` if and only if:
1. `support_rate >= 0.70` (at least 70% of claims are verifiable), AND
2. `fabricated_citations == 0`, AND
3. `cross_product_mismatches == 0`, AND
4. `numeric_mismatches == 0`.

---

## 5. Benchmark Evaluation Results

The evidence validation engine was evaluated using `scripts/evaluate_evidence_validation.py` against a curated fixture dataset (`tests/fixtures/evidence_validation_cases.json`, N=15):

| Metric | Result | Target / Standard |
| :--- | :---: | :---: |
| **Sample Size (N)** | **15** | Curated benchmark set |
| **True Positives (Hallucinations Caught)** | **7** | Fabricated IDs, cross-product citations, numeric mismatches, ungrounded claims |
| **True Negatives (Valid Claims Supported)** | **8** | Ground-truth supported claims, valid tolerances, unverifiable specs/NULLs |
| **False Positives (Valid Claims Rejected)** | **0** | 0% false alarms |
| **False Negatives (Hallucinations Missed)** | **0** | 0 missed violations |
| **Precision** | **100.0%** | All flagged claims were genuinely invalid |
| **Recall** | **100.0%** | All constructed invalid claims were caught |
| **False-Positive Rate** | **0.0%** | Valid claims were never improperly rejected |
| **Overall Accuracy** | **100.0%** | Perfect categorization across test set |

### Breakdown by Failure Mode
- **Supported Claims:** 6/6 correct (100.0%)
- **Fabricated Citation IDs:** 1/1 correct (100.0%)
- **Cross-Product Mismatches:** 1/1 correct (100.0%)
- **Numeric Price Mismatches:** 1/1 correct (100.0%)
- **Numeric Rating Mismatches:** 1/1 correct (100.0%)
- **Numeric Review Count Mismatches:** 1/1 correct (100.0%)
- **Unverifiable Specifications (Missing in Catalog):** 1/1 correct (100.0%)
- **Unverifiable Catalog Fields (NULL Database Values):** 1/1 correct (100.0%)
- **Low-Similarity Ungrounded Assertions:** 2/2 correct (100.0%)

---

## 6. Critical Limitations and Scientific Scope

In accordance with Section 0.2 of `docs/master-spec.md` and Phase 6 requirements:

1. **Curated Benchmark Scope:**
   These metrics represent performance on a curated, deterministic offline evaluation suite. **100% precision and recall on this benchmark must NOT be cited as proof of universal factual correctness or zero hallucination across unrestricted natural-language queries.**
2. **Deterministic vs Heuristic Boundary:**
   - Exact ID verification and SQLite database recomputations are deterministic and provide rigorous mathematical guarantees within stated tolerances.
   - Lexical overlap (Jaccard) and embedding cosine similarity are heuristic approximations. They reliably identify blatant topical drift (e.g., citing a yoga mat review for a backpack claim), but cannot reliably catch subtle semantic contradictions, logical fallacies, or negative polarity switches without deeper NLI (Natural Language Inference).
3. **Database Completeness Dependency:**
   Verification is bounded by the completeness of the SQLite product catalog. Missing specifications produce `UNVERIFIABLE` to avoid false rejections, meaning incomplete catalog data yields unverified, rather than validated, knowledge.
