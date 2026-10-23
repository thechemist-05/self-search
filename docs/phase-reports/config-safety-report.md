# Dataset Integration, Phase 1 Report: Index Configuration and Manifest

Scope: configuration safety only. No datasets were downloaded or ingested, and no adapter, price-estimate or experimental-dataset work was started. Design details are in [rag-design.md §7](../rag-design.md#7-index-manifest-and-startup-check-configuration-safety-phase) and [deployment.md](../deployment.md#vector-indexes-must-match-the-database).

## What changed
- **`SHELFSENSE_VECTOR_STORE_DIR`** (default `data/vector_store`) sets the index directory.
  - The per-file variables `PRODUCT_INDEX_PATH`, `REVIEW_INDEX_PATH` and `VECTOR_STORE_PATH` still win.
  - The default store is now created on first use, not at import.
- **`src/vector_store/manifest.py`** provides:
  - a catalogue fingerprint over products and reviews (personal tables excluded);
  - an index/database consistency check;
  - an atomic manifest write, done only after a passing check;
  - startup verification.
- **The API refuses to start** if the manifest is missing or doesn't match the database. A controller injected by tests skips the check.
- **`scripts/generate_embeddings.py`:**
  - reads `SHELFSENSE_DB_PATH`, read-only (the stale `SHELFSENSE_DB` is gone);
  - uses one embedder per build, and records it;
  - builds in a temporary directory, validates, and swaps the files in with the manifest last;
  - restores the previous files on failure;
  - refuses a non-empty directory unless `--overwrite` is passed.
- **`/populate`** uses the same clean rebuild. It previously appended, which duplicated vectors. Its response is unchanged.
- **`scripts/verify_index_manifest.py`:** `--check` only reports; `--write` writes only for a consistent pair.

## Current indexes
- **Read-only check:**
  - `products.index`: 20 of 20 products;
  - `reviews.index`: 100 of 100 reviews;
  - no duplicates, nothing unindexed, both 64-d.
- **Manifest:** written to `data/vector_store/manifest.json`, the only change in that directory.
- **Legacy `faiss.index`:** 140 vectors, 100 map entries, 40 duplicate ids. Recorded as excluded and not modified (decision: Option A). See [limitations.md](../limitations.md).

## Verification
- **Full suite**, three runs plus one more after the doc changes: each run had **637 passed, 1 skipped (Ollama), 0 failed**. 55 tests are new.
- **Mutation check:** with the startup and consistency checks disabled, 12 of the new tests fail, as they should.
- **Live run on copies (port 8601):**
  - a valid manifest served `/health` and `/chat`;
  - an edited product title made uvicorn exit at startup with `vector_index_manifest_invalid`.
- **Checksums:**
  - the production database, the v1 backup and all six original index files are byte-identical;
  - `src/evidence`, `src/db`, `src/retrieval`, `src/agent`, `src/personal`, `src/llm` and `frontend/` are unchanged;
  - the database's logical fingerprint is identical across 15 tables.

## Changed test
`tests/integration/test_agent_api.py::test_default_controller_wiring` now stubs the manifest check and asserts it is called. It builds the real controller on an empty temporary database, which the new check rightly refuses.
