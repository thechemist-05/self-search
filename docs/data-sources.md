# Data Sources

All datasets used by Self Search are documented here with source, licence, and processing notes.

| Name | URL | Licence | Downloaded | Rows | Notes |
|------|-----|---------|------------|------|-------|
| `test-fixture` (hand-made sample) | — (in repository: `tests/fixtures/sample_data.json`) | Project-authored test data | n/a | 20 products, 100 reviews | The **only** data currently in `data/processed/shelfsense.db` |

## Current state (verified 2026-10-19)

**No external dataset has been ingested.** Phase 1's dataset selection and ingestion of a real, licensed dataset (e.g. Amazon Reviews 2023) was not completed. `data/raw/` is empty, and the `data_sources` table holds one row, `test-fixture`, with no URL or licence.

What the sample catalogue contains, and what it cannot support:

| Aspect | Fact | Consequence |
|---|---|---|
| Products | 20 hand-written items | Most real queries find nothing; the UI says so instead of padding results |
| Categories | Only `Sports_and_Outdoors` (10) and `Electronics` (10) | Too coarse for type requests ("shoes"), so product types are matched against titles and descriptions instead |
| Footwear | One item: *Marathon Running Shoes - Men*, USD 89.99. No sneakers, no footwear category, no "everyday use" attribute | A request like "sneakers under 4000 rupees" can at best surface this one shoe |
| Currency | Every price is **USD** (12.99–89.99). No INR prices, no exchange-rate data | Budgets in other currencies are **not converted**; the user is asked to restate the budget in USD |
| Image URLs | All point to `example.com` (placeholders) | The UI always shows a neutral "No image available" graphic |
| Reviews | 5 per product, hand-written | `rating_count` (e.g. 1,250) comes from the fixture's product record, not from local reviews; the UI labels them separately |
| Review analysis | `review_analysis` and `product_aspect_summary` are empty (Phase 2 not implemented) | "Aspect analysis not available" |

Supporting real queries (Indian-rupee budgets, real footwear ranges) requires ingesting a properly licensed dataset with currency-tagged prices. That is a separate data decision outside the Phase 9 relevance fix.

## Expanded Fashion catalogue: selection-v3, 50,000 products (2026-10-22)

**Build:** `scripts/build_experimental_db.py --run-id fashion-v3-20261022T085353Z --selection config/selection_amazon2023_v3.toml`.
- **Source:** the same verified local files as v2: Amazon Reviews 2023 `Amazon_Fashion`, revision `2b6d039e…`, with checksums checked again before use. Nothing was downloaded again.
- **Output:** `data/experiments/fashion-v3-20261022T085353Z/`. The v2 catalogue (`fashion-v2-20261021T094306Z`, 20,000 products) is unchanged.

**Capacity:** under the unchanged v2 review rules (at least 3 reviews of 20+ characters each, at most 8 kept), 61,313 products qualified under the v2 types. v3 adds seven types and duplicate removal, and selects exactly **50,000** of the qualifying products.

**What v3 changes** (see the file header of `config/selection_amazon2023_v3.toml`):
- The quota is 50,000.
- **The same listing under several ASINs counts once:** identical normalised titles keep the copy with the most qualifying reviews. Different ASINs with different titles stay distinct, and a duplicate ASIN was already counted once.
- **Category-wide exclusions** for shoe- and bag-care items: cleaners, erasers, polish, lace locks, heel grips and similar.
- **Seven more genuine Fashion types:** skirts, shorts, sweaters, leggings, swimwear, hats and caps, belts. Electronics stays excluded.

**Result** (validated by the build; all critical checks passed):

| | |
|---|---|
| Products / reviews | **50,000 / 271,939** (3 to 8 per product; 95.3% verified purchases) |
| Orphan reviews, duplicate product or review ids, review/source mismatches | 0 |
| Prices | 3,451 USD source prices (6.9%), **no INR prices**; 7,454 synthetic INR estimates (14.9%), none on a priced product |
| Coverage | descriptions 5.6%, brand field 1.0% (store names are far more common), ratings 100% |
| Schema | v4 (no profile tables) with the experimental `price_estimates` marker; `integrity_check` ok |
| Products per type | jackets, dresses_kurtas, shirts, jeans_trousers, hats_caps 4,405 each; tshirts 4,404; handbags 4,057; swimwear 2,990; other_shoes 2,443; sweaters 2,393; shorts 2,241; skirts 1,951; boots 1,641; belts 1,637; sandals 1,429; leggings 1,052; sneakers 918; backpacks 774; running_shoes 45 (all that qualify) |
| Indexes | `vector_store/`: 50,000 product and 271,939 review vectors, 64-d `offline-feature-hash-v1`, manifest consistent |
| Image URLs | 49,532 products have a product photo URL (`product_images.json`); the rest have only Amazon's blank placeholder |

## Expanded catalogue: selection-v4, 86,588 products (2026-10-23)

**Build:** `scripts/build_expanded_db.py --run-id fashion-v4-20261023T014937Z --base data/experiments/fashion-v3-20261022T085353Z --selection config/selection_amazon2023_v4_additions.toml`, followed by `scripts/validate_product_images.py` and the index build.

**Inputs:**
- **Source:** Amazon Reviews 2023 (McAuley Lab), revision `2b6d039e…`, the same dataset and rights as v3: no licence stated, so local academic use only, with attribution and no redistribution.
- **New category files** (downloaded with `scripts/fetch_amazon2023.py`, sha256 checked before use): `Clothing_Shoes_and_Jewelry` (metadata 17.96 GB, `e5ff9085…`; reviews 27.81 GB, `150eb2a9…`), `All_Beauty`, `Handmade_Products` and `Sports_and_Outdoors`.
- **Base:** the 50,000-product v3 database, copied with SQLite's backup API. The v3 directory is unchanged (sha256 `094418a2…`, verified after the build).
- **Output:** `data/experiments/fashion-v4-20261023T014937Z/`. It contains `shelfsense.db`, `vector_store/`, `dataset_manifest.json`, `product_image_candidates.json`, `product_images.json` and `image_report.json`.

**Selection rules** (`config/selection_amazon2023_v4_additions.toml`, version `selection-v4-allocation`):
- **Typing:** each addition is typed from its own title by sub-style rules. For example, `saree_banarasi`, `lehenga_bridal`, `earrings_jhumka` and `mens_kurtas`, where `mens_*` requires the word "men".
- **Allocation:** within a category, products are allocated evenly across sub-styles. Nothing is borrowed from another category to fill a shortfall.
- **Reviews:** at least **one** review of 20+ characters is required (v3 required three), and at most 8 are kept. Verified purchases come first, then helpful votes.
- **Duplicates removed:** exact ASIN, identical normalised title, or near-duplicate title within the same store, all checked against the base and within the additions.

**Allocation vs. result:**

| Category | Requested | Added | Shortfall | Similar products already in v3 |
|---|---:|---:|---:|---:|
| Sarees | 8,000 | **1,378** | 6,622 | 3 |
| Lehengas | 7,000 | **210** | 6,790 | 0 |
| Earrings | 6,000 | 6,000 | 0 | 1 |
| Jewellery beyond earrings | 4,000 | 4,000 | 0 | 124 |
| Men's fashion | 5,000 | 5,000 | 0 | 3,754 |
| Footwear | 7,000 | 7,000 | 0 | 4,023 |
| Bags & accessories | 5,000 | 5,000 | 0 | 6,937 |
| Beauty & personal care | 4,000 | 4,000 | 0 | 76 |
| Home & lifestyle | 3,000 | 3,000 | 0 | 271 |
| Sports & fitness | 1,000 | 1,000 | 0 | 1,360 |
| **Total** | **50,000** | **36,588** | **13,412** | |

- **Why the shortfall:** these are every saree and lehenga in the source that has a usable review and is not a duplicate.
  - Clothing metadata typed and passed to the review pass: 214,143 products. Of these, 207,942 had a qualifying review.
  - After deduplication only 1,381 sarees remain (1,378 new, plus 3 already in v3) and 210 lehengas.
  - No other legitimate source was available under the project's rules, so the target of 100,000 was not reached. Nothing was invented to fill the gap.
- **Duplicates removed:**

  | | Count |
  |---|---:|
  | Identical title already in v3 | 553 |
  | Near-duplicate of a v3 product | 442 |
  | Identical title within the additions | 106,454 |
  | Near-duplicate within the additions | 7,927 |

**Result** (validated by the build; no problems reported):

| | |
|---|---|
| Products / reviews | **86,588 / 531,796** (1 to 8 per product; 96.8% verified purchases) |
| Orphan reviews, duplicate product, review or title ids, invalid prices, personal-data markers | 0 |
| Prices (source) | 29,502 USD source prices (34.1%), all converted to INR at the documented rate; 57,086 without a source price |
| Prices (estimates and none) | 8,622 synthetic INR estimates (10.0%), none on a priced product; 48,464 products (56.0%) have no price at all |
| Coverage | descriptions 22.3%, brand field 8.0%, ratings 100% |
| Traceability tables | `product_provenance`, `catalogue_types` and `price_normalization` (original price, currency, rate, rate date, source), one row per product |
| Schema | v4 with the experimental `price_estimates` marker; `integrity_check` ok |
| Indexes | `vector_store/`: 86,588 product and 531,796 review vectors, 64-d `offline-feature-hash-v1`, manifest consistent. Refuses the v3 database. |
| Images | 84,272 products (97.33%) have a validated photo; details below |

**Product images** (`scripts/validate_product_images.py`; report in `image_report.json`):
- **Candidates:** for each product, up to the first few `large` image URLs from its own source record. Matching is by exact `parent_asin` only.
- **Validation:** every candidate URL was requested over HTTP (149,224 URLs). A URL is kept only if it returns `image/jpeg`, `png` or `webp` of at least 1,500 bytes.
- **Rejected:** URLs shared by 5 or more unrelated products are rejected as stock or placeholder images (7,207 candidate URLs). So are 87 empty or placeholder responses and 2 HTTP 404s.
- **Coverage:** 97.33% overall. Sarees 1,376 of 1,378 (99.9%), lehengas 209 of 210 (99.5%), earrings 5,999 of 6,000.
  - Some older v3 shoe types are below 95%: boots 88.7%, sneakers 90.6% and sandals 90.8%.
- **Fallback:** 82,632 products have a second valid photo, which the UI uses if the first fails to load. Products without one show "Image unavailable".
- **Rights:** URLs only, as for v3; no image is downloaded or stored.

## Product image URLs for the Fashion catalogue (2026-10-22)

The adapter drops the source `images` field, so `products.image_url` is empty in the Fashion database. To show product photos **without changing the database**, `scripts/extract_product_images.py` reads the already-downloaded metadata file `raw/meta_categories/meta_Amazon_Fashion.jsonl` and writes `data/experiments/fashion-v2-20261021T094306Z/product_images.json`.
- **Verification:** the file's sha256 is checked against `SOURCE.json` first (`629dde62…`).
- **Matching:** by exact `parent_asin` = `products.external_id` only.
- **Image chosen:** the `large` size of the `MAIN` image, else of the first image.
- **Coverage:** all 20,000 selected products have image entries, and **19,826 have a usable photo URL**. 174 have only Amazon's blank 1-pixel placeholder (one `.gif` shared by 174 unrelated products), which is skipped.
- **What is stored:** URL references only, all on `m.media-amazon.com`. No image is downloaded, stored or redistributed by the project. The UI fetches an image only when remote images are enabled, at display time.
- **Rights:** the images belong to their owners, and the dataset states no licence. Showing them is limited to the local academic demonstration, with the same attribution as the dataset. Remove `product_images.json` or run with `-NoImages` to show none.

## Source adapters (dataset phase 2: code only, nothing ingested)

`src/data/adapters/` turns dataset records that are already on disk into catalogue rows. Nothing is downloaded. The only data used so far is hand-written fixtures in `tests/fixtures/adapters/`, which are clearly not real.

| Adapter | Source | Rights as audited (2026-10-21) | Prices |
|---|---|---|---|
| `amazon_reviews_2023.py` | McAuley Lab, Amazon Reviews 2023 (`parent_asin` ids) | **No licence stated by the dataset.** Local academic prototyping only, with attribution; raw files are not redistributed and no unrestricted rights are claimed | USD (amazon.com, at crawl time), only when the source gives a plain positive number; never converted |
| `abo.py` | Amazon Berkeley Objects, amazon.in listings only | CC BY 4.0. Credit: data by Amazon.com; dataset by Guillaumin, Dideriksen, Deng, Arora (Amazon.com), Collins and Malik (UC Berkeley) | none |

**Rules enforced in `pipeline.py`:**
- **Missing values stay missing.** NULL is inserted explicitly, so the schema's `currency` default of `'USD'` never applies.
- **Brand:** only from a source brand field (`details.Brand`, or ABO `brand`). A storefront name is kept as an attribute, never as a brand.
- **Reviews:** attached only on an exact match with a product id from the same source. Never by case-insensitive match, by title, or across sources. ABO and Amazon Reviews 2023 are never linked.
- **Personal data:** reviewer ids and names, and review images, are dropped inside the adapter.
- **Review ids:** Amazon Reviews 2023 has none, so the id is a hash of the review's own fields (product, variant, timestamp, title, text).
- **Rejections:** every rejected record is counted with a reason, with up to 20 sample references per reason. The report never contains personal data.
- **Deduplication:** within a run, by the source id (the first occurrence wins). Re-runs insert nothing.
- **Per-product review cap (optional):** keeps verified reviews first, then by helpful votes, then newest.
- **Transactions:** one transaction per run, including the `data_sources` row.

## Prices in INR: documented exchange rate (2026-10-23)

Every price Self Search shows or filters is in rupees. The source prices are amazon.com prices in USD (the Amazon Reviews 2023 crawl), so they are converted. Conversion uses **only** the rate in `config/exchange_rates.toml` (version `fx-ecb-20230929-v1`):

| | |
|---|---|
| Rate | 1 USD = ₹83.0815 |
| Date | 2023-09-29, the last ECB working day of the September 2023 data period |
| Source | European Central Bank euro reference rates: INR per EUR 88.0165 (series `EXR.D.INR.EUR.SP00.A`) and USD per EUR 1.0594 (`EXR.D.USD.EUR.SP00.A`); USD→INR = 88.0165 / 1.0594 |
| Retrieved | 2026-10-23 from `https://data-api.ecb.europa.eu/service/data/EXR/D.USD+INR.EUR.SP00.A?startPeriod=2023-09-29&endPeriod=2023-09-29&format=csvdata` |
| Rounding | whole rupees, half up (`int(price × rate + 0.5)`), identical in Python and SQL |

- **Never invented:** `src/pricing/fx.py` refuses the file if a field is missing, the quote is not INR, the rate is implausible, or the stored rate differs from the ECB quotient. A currency without an entry is never converted, and its products show "Price unavailable".
- **Originals kept:** the original price and currency stay in `products` (and in `price_normalization` in the expanded catalogue) for traceability. They are never shown or sent to the model.
- **Separate from estimates:** converted prices are labelled as conversions of the source price, never as retailer INR prices, and are kept apart from synthetic estimates, which still apply only to products with no source price.

## Synthetic INR price estimates (experimental, off by default)

`src/pricing/` with `config/price_estimate_ranges.toml`:
- **The ranges are illustrative assumptions written by hand, not market data.**
- **Who gets an estimate:** only a product whose source supplied no price. The estimate is a deterministic point in the first matching range, from `sha256(version | source | external_id)`, rounded to ₹10.
- **Price status:** `budget.py` classifies each price as *verified*, *estimated* or *unknown*. A budget is met only in its own currency: verified INR, or an INR estimate when enabled. USD and unknown prices never satisfy an INR budget, and nothing is converted.
- **Not connected yet:** retrieval, the API and the UI don't use any of this. That is a later, separately approved phase.


## Downloaded raw files (dataset phase 3, profiling only)

| Dataset | Revision | Files | Licence status | Downloaded | Used for |
|---|---|---|---|---|---|
| Amazon Reviews 2023 (McAuley Lab) | `2b6d039e…` | `meta_Amazon_Fashion.jsonl`, `Amazon_Fashion.jsonl` (2.47 GB, sha256 verified) | No licence stated; local academic use; not redistributed | 2026-10-21 | Profiling only. Nothing is ingested yet (see `docs/phase-reports/dataset-phase3-report.md`) |
