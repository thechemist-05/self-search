# Dataset Integration, Phase 2 Report: Adapters, Experimental Schema v3, INR Estimator

Scope: code and tests using hand-written fixtures only. No dataset was downloaded, no external file was read, and no real records were ingested. Details are in [data-sources.md](../data-sources.md) and [database-schema.md](../database-schema.md).

## Built
- **`src/data/adapters/`:**
  - a shared `SourceAdapter` interface (`base.py`);
  - `amazon_reviews_2023.py` and `abo.py` (amazon.in only);
  - `pipeline.py`, which provides:
    - validation and deduplication;
    - exact review-to-product matching within a source;
    - explicit NULLs for missing values;
    - provenance;
    - an optional per-product review cap;
    - one transaction per run;
    - an idempotent re-run;
    - a rejection report with reasons.
- **`src/db/experimental_schema.py`:** the `price_estimates` table (v3).
  - It is applied only by an explicit call, and not through `MIGRATIONS`.
  - It refuses the production database, the backups in `data/processed/`, in-memory databases and the wrong version.
  - Re-applying is a no-op.
- **`src/pricing/estimates.py`:** loads the versioned config (`config/price_estimate_ranges.toml`, marked as illustrative assumptions). Estimates are deterministic, given only to products without a source price, and never write to `products`.
- **`src/pricing/budget.py`:**
  - classifies prices as verified, estimated or unknown;
  - makes budget decisions that never convert currencies;
  - keeps estimates hidden unless enabled.

  It is **not yet wired** into retrieval, the API or the UI.
- **Setting:** `estimated_prices_enabled` (default `False`) and `price_estimate_config`.
- **Amendment:** the master spec (constraint 8 and the AI rules) and `AGENTS.md` now carry the approved synthetic-estimate exception.

## Verification
- **Full suite**, three runs: each **711 passed, 1 skipped (Ollama), 0 failed**. 74 tests are new.
- **Mutation checks** (each fault injected deliberately; every one was caught):
  - estimates shown while disabled: 2 tests fail;
  - estimates given to products with a verified price: 4 fail;
  - case-insensitive review matching: 4 fail.
- **Protected files byte-identical:**
  - the production database and its v1 backup;
  - all six index files and the Phase 1 manifest (still valid);
  - `src/evidence`, `src/api`, `src/db` (connection, schema, migrations), `src/retrieval`, `src/agent`, `src/llm`, `src/personal`, `src/vector_store`, `scripts/` and `frontend/`.

  The production database is still at v2 with no `price_estimates` table, and its logical fingerprint is identical across 15 tables.

## Bugs found while testing (fixed before the final runs)
- **Estimator re-run:** with a `sqlite3.Row` row factory, "unchanged" estimates were rewritten on every run, because a `Row` never equals a tuple. Re-runs now compare as tuples.
- **Malformed JSONL lines:** they reached the adapters as `missing_parent_asin`. Adapters now reject them as `malformed_record`.
