# Dataset Integration, Phase 3 Report: Fashion Download, Verification and Profiling

Scope: the Amazon Reviews 2023 **Amazon_Fashion** files only. No database was built or migrated, no estimates were generated, and no index was built. Electronics was not downloaded.

## Download and verification
| File | Bytes | sha256 (published = verified) | Retrieved (UTC) |
|---|---|---|---|
| `raw/meta_categories/meta_Amazon_Fashion.jsonl` | 1,422,365,805 | `629dde620a4001934a8af705f6e5514a1b2d349720c26876ee803b90051a62df` | 2026-10-21 08:42 |
| `raw/review_categories/Amazon_Fashion.jsonl` | 1,051,324,731 | `e597b3ce6cbb2f9994a32e0eee6bb04fdae50725f2d49c89f6baef1a7b986c57` | 2026-10-21 08:54 |

- **Source:** `https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023`, revision `2b6d039ed471f2ba5fd2acb718bf33b0a7e5598e`.
- **Licence status:** no licence stated. Local academic prototyping only, with attribution; the files are not redistributed.
- **Location:** `data/raw/amazon_reviews_2023/<revision>/`, which is git-ignored. `SOURCE.json` records the provenance.
- **Verification:** each file was checked by the downloader and again independently with `sha256sum`.

## Product metadata (826,108 lines)
- **Records:** 0 malformed lines, 0 duplicate `parent_asin`, 58 rejected for a missing title; 826,050 accepted.
- **Coverage of accepted products:**

  | Field | Coverage |
  |---|---|
  | title | 100% |
  | average rating | 100% |
  | rating count | 100% |
  | features | 56.1% |
  | description | 7.2% |
  | `details.Brand` | 5.1% |
  | source price | 6.1% (50,244 products) |
  | source category list | **0%**: every record has an empty `categories`; only `main_category` "AMAZON FASHION" exists |

- **Prices:** all 50,244 are plain numbers, labelled USD (amazon.com). There are **0 genuine INR prices**. The 43 "INR marker" hits are model numbers or "Rs" inside product titles, and none of those products has a price.

## Reviews (2,500,939 lines)
- **Exact-id matching:** 0 malformed lines, 0 missing `parent_asin`, and **100% of reviews match a product id exactly**.
- **Reviews of candidate products:** 912,562.
  - **Qualifying:** 805,113 (valid rating and at least 20 characters), of which 93.0% are verified purchases.
  - **Excluded:**

    | Reason | Count |
    |---|---|
    | Short text | 98,229 |
    | Missing text | 900 |
    | Duplicate | 8,320 |

  - The duplicate check covers the first 64 distinct reviews of each product.

## Selection (rules `selection-v1`, sha256 `3af8a74e…`)
- **Pool:** 362,265 candidate products (typed by title keywords); 63,856 of them have at least 3 qualifying reviews.
- **Approved Fashion quota (12,000):** 12,000 products and **83,744 reviews** (up to 8 per product; median 8; 8,149 products have 8).
- **Gap to the 20,000 / 100,000 targets:** 8,000 products and 16,256 reviews, the share planned to come from Electronics.
- **Fashion-only option (quota 20,000, same rules):** 20,000 products and 130,197 reviews. This would mean no electronics products at all.
- **Thin types:** running shoes have 48 qualifying products, backpacks 896 and sneakers 1,115.
- **Coverage among the 12,000 selected products:**

  | Field | Coverage |
  |---|---|
  | description | 8.2% |
  | brand | 3.4% |
  | USD price | 10.7% |
  | features | 73.7% |

- **Type precision (spot check of 72 random selected titles):** about 83% correct. Errors include socks typed as boots or shoes, an earring typed as a jacket, watches typed as dresses, and T-shirt dresses typed as shirts. Types are used only to balance the selection; they are not stored as product facts.

## Storage
- 2.4 GB raw data and 11 MB of profile output, in `data/experiments/profile-20261021T085455Z/`.
- 128 GB free.

## Download speed
- Measured 0.3–1.5 MB/s. In practice the Fashion files took about 24 minutes (1.42 GB) and 12 minutes (1.05 GB).

## Revision: selection-v2 (Fashion only, quota 20,000)

`config/selection_amazon2023_v2.toml` (sha256 `06db2f12…`) is a new version. v1 is unchanged and is still verified against the sha256 recorded in its profile.

- **Quota:** 20,000 Fashion products. Electronics is not used.
- **Review rules:** unchanged (3 qualifying reviews of at least 20 characters; at most 8 kept per product).
- **New exclusions:** category-wide exclude phrases for accessories, chosen from sampled titles: socks, "shoe size", jewellery, watches, keychains, charms, shoelaces, insoles, packing cubes, organizers, wallet-only phrases such as "bifold" and "card holder", "dress gloves" and "dress hat".
- **Kept on purpose:** ambiguous words that mostly appear in genuine apparel (belt, cap, costume, leggings, ring, scarf, hat, gloves, apron, and singular "sock").
- **Re-typing:** T-shirt and shirt dresses are now typed as dresses.

**Result** (profile in `data/experiments/profile-v2-20261021T090628Z/`):
- **Products and reviews:** 353,160 candidates, 61,313 qualifying, **20,000 selected and 126,977 reviews**. Both targets are met.
- **Effect of the tighter rules:** at the same 20,000 quota, v1 would give 130,197 reviews, so v2 costs 3,220 reviews.
- **Changes from v1:** 2,543 v1-qualifying products are removed and 1,464 are re-typed (1,452 of them shirt or T-shirt dresses moved to dresses).
- **Accuracy of the removals:** in a random 25, 23 are genuinely not the claimed type. The two wrongly removed were water shoes titled "…Shoes Socks…" and a "T Shirt & Stickers" bundle.
- **Accuracy of the selection:** a random 120 (10 per type) were about **96% correct** (115 of 120), against about 83% for v1. Remaining errors: cord locks typed as backpacks, a belt "for jeans" typed as trousers, grocery tote bags typed as handbags, a "Tee Shirt Short Dress" typed as T-shirts, and a girls' dress set typed as shirts.

| Type | Candidates | Qualifying | Selected | Reviews |
|---|---|---|---|---|
| sneakers | 7,024 | 994 | 994 | 4,216 |
| running_shoes | 586 | 45 | 45 | 196 |
| sandals | 11,225 | 1,576 | 1,576 | 7,142 |
| boots | 11,940 | 1,809 | 1,809 | 8,369 |
| other_shoes | 14,764 | 2,646 | 2,109 | 11,271 |
| backpacks | 2,192 | 810 | 810 | 4,413 |
| handbags | 15,128 | 4,264 | 2,110 | 15,079 |
| tshirts | 51,722 | 5,541 | 2,109 | 14,044 |
| shirts | 55,666 | 8,305 | 2,109 | 16,141 |
| jeans_trousers | 42,843 | 7,267 | 2,109 | 15,527 |
| dresses_kurtas | 104,072 | 22,994 | 2,110 | 16,880 |
| jackets | 35,998 | 5,062 | 2,110 | 13,699 |
| **Total** | 353,160 | 61,313 | **20,000** | **126,977** |

Sneakers, running shoes, sandals, boots and backpacks are selected in full; their supply is the limit. The remaining share is split evenly (about 2,110 each) across other shoes, handbags and the apparel types.
