# Dataset Integration, Phase 4 Report: Experimental Fashion Catalogue, Indexes, INR Estimates

## Built (`data/experiments/fashion-v2-20261021T094306Z/`)

**Database** (`scripts/build_experimental_db.py`):
- **Source:** the verified Amazon_Fashion files (revision `2b6d039e…`), selected with `selection-v2`.
- **Contents:** **20,000 products and 126,977 reviews**, identical to the profile. Schema v3 (`price_estimates`).
- **Size:** 96.6 MB. The build took 581 s.
- **Review exclusions:**

  | Reason | Count |
  |---|---|
  | Over the per-product cap of 8 | 146,118 |
  | Shorter than 20 characters | 30,774 |
  | Duplicate | 3,191 |
  | Empty text | 312 |

- **Validation:** 0 orphan reviews, 0 source mismatches, 0 duplicate product or review ids. FTS counts equal the table counts, `integrity_check` is ok, every product has 3–8 reviews, no reviewer data is stored, currencies are USD (1,648) or none (18,352), and no product with a source price has an estimate.
- **Field coverage:**

  | Field | Coverage |
  |---|---|
  | Average rating | 100% |
  | USD price | 8.2% |
  | Description | 7.1% |
  | Brand | 1.6% |
  | Verified-purchase reviews | 96.0% |

- **Synthetic INR estimates:** 6,454 products (`illustrative-inr-v1`). 11,898 have no matching range, and 1,648 have a verified USD price, so they get no estimate.
- **Report:** counts, exclusions, coverage and source checksums are in `ingest_report.json`.

**Indexes** (`vector_store/`):
- `products.index`: 20,000 vectors, 5.3 MB.
- `reviews.index`: 126,977 vectors, 33.5 MB.
- **Embedder:** `offline-feature-hash-v1 (64-d)`; Ollama is not installed.
- **Consistency:** no duplicates, no missing or extra rows, one dimension. The manifest was written only after these checks passed and still validates after live use.

## INR integration (off by default, `SHELFSENSE_ESTIMATED_PRICES_ENABLED`)

**Changes:**
- `MetadataFilter.use_price_estimates` is set per turn by the router. With it, an INR bound is met by a verified INR price, or by an estimate for a product with **no source price**. USD and unknown prices never match.
- Retrievers strip the internal estimate value before returning products.
- `ProductCard.price_status` and `estimated_price` are new, additive fields. `price` and `currency` are unchanged.
- The constraint report gains `uses_synthetic_estimates` and per-card "Synthetic estimate INR … (illustrative estimate, not a retailer price)" checks.
- The UI shows "≈ INR … (synthetic estimate)", a warning chip and a caption, in cards and in comparisons.

**Untouched:** the evidence validator, prompts and the personal ranking code. Estimates never reach them.

## Defects found and fixed during this phase
- **₹ broke requests on Windows.** A "₹" in a query raised `UnicodeEncodeError` while the API logged it (the console or log file uses cp1252), so the request returned HTTP 500. Fixed by `make_console_output_safe()` (backslash escapes); regression tests added. Verified live: ₹ queries return 200 and the log shows `₹`.
- **Long feature chips overflowed on phones.** Long source feature sentences overflowed chips at 390 and 320 px. Fixed: chips wrap within the card, and feature chips are shortened to 60 characters.

## Verification
- **Full suite**, three runs: **798 passed, 1 skipped (Ollama), 0 failed**.
- **Fault injection:** deliberately breaking seven protections in temporary copies of the real files made tests fail each time. The faults were:
  - an estimate overriding a source price;
  - ignoring the enabled flag;
  - showing estimates while disabled;
  - leaking the estimate key from the keyword retriever;
  - letting USD satisfy an INR budget in SQL;
  - dropping the UI label.
- **Live smoke** (8601 with estimates on, 8603 with them off): 24 of 24 checks passed, including the five requested scenarios, review question-answering, comparison, personal ranking, and no estimate appearing in any answer, evidence or validation report.
- **Browser checks:** headless Chrome at five viewports on 8602, 40 of 40 steps, 0 problems.
- **Protected files:** the production database, its v1 backup, all six index files and the manifest are byte-identical. Production is still schema v2. `src/evidence` is unchanged and `verify_api_key` is unchanged.

## Limitations
See [limitations.md](../limitations.md#experimental-fashion-catalogue-and-synthetic-inr-estimates-dataset-phase-4). In short:
- USD-only source prices on 8.2% of products.
- Estimates are illustrative, with keyword misfires on accessories.
- Apparel has no estimates.
- "Sneakers" maps to the broad footwear type.
- Ollama is not installed, so only the offline fallback was tested.
- The legacy `/ask` returns 500 without Ollama.
- The validator does not extract ₹ amounts.
- After a compare turn, the sidebar price chip shows no currency (existing behaviour).
