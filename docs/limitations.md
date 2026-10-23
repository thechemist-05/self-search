# Known Limitations and Risks

*To be expanded throughout development. Honest reporting of limitations is required.*

## Offline answers are low-support and not production-ready

When Ollama is unavailable, product answers come from the Phase 5 safe-fallback template, not the model. The Phase 6 `EvidenceValidator` still checks every one of them, and they score poorly:

| Measurement (2026-10-19, 20-product fixture DB) | support_rate | is_valid | Flagged claims |
|---|---|---|---|
| Search "wireless earbuds under $100" (live uvicorn) | 0.56 | false | 4 / 6 |
| Question on product 2 (live uvicorn) | 0.40 | false | 2 / 4 |
| Range across all offline smoke turns (Phase 7) | 0.33 – 0.56 | false | — |

Unsupported claims are shown with an `[UNVERIFIED]` prefix (flag mode). This is the intended behaviour: the validator is **not** bypassed or relaxed to make offline answers look better. The offline fallback is a degraded mode for demos without a model. It is not a production-quality answer path.

## Security model

- **No user accounts or profiles** (removed 2026-10-21). Self Search stores no personal data, so there is no per-user data to protect or authorise.
- **Only one access control exists:** the optional, shared `API_KEY` header. It is off by default.
- **Network exposure (fixed 2026-10-19).** `docker-compose.yml` now publishes both ports on `127.0.0.1` only, and a test enforces it. Direct uvicorn launches bind to `127.0.0.1`, verified as refused on the LAN address. A manual `docker run -p 8000:8000` would still expose all interfaces. See [deployment.md](deployment.md). `API_KEY` remains unset by default for local use and must be set before any wider exposure.
- **Concurrency (defect found in Phase 9, fixed with approved Option A).** The API shares one SQLite connection across worker threads. Before the fix, overlapping requests got wrong or empty results (1,833 of 4,000 concurrent queries). Now:
  - The connection is a `SerializedConnection` (`src/db/connection.py`): every statement **executes and fetches all its rows** under one re-entrant lock, so no other thread can interleave with an active query.
  - Explicit transactions hold the same lock from SAVEPOINT to RELEASE.
  - There is one lock, and it is re-entrant, so there is no lock-ordering deadlock.

  **Trade-off:** all database access in the API is serialised. Statements are short and LLM generation runs *outside* the lock, so a slow answer does not block other requests' database access. Heavy concurrent load still queues on the database, which is acceptable for a local single-user prototype.

  **Remaining limits:**
  1. Each statement and each write transaction is atomic, but a multi-query read is not one snapshot. A concurrent write between its queries can give a mix of before and after (each part correct).
  2. In-memory `SessionState` is not locked. Two simultaneous requests on the *same* session can interleave filter and history updates.
  3. Scripts outside the API (ingestion, migration) use ordinary single-threaded connections. Running them while the API serves requests relies on SQLite's file locking.

## Catalogue and relevance limits (Phase 9 relevance fix)

- **Sample data only.** The catalogue is 20 hand-written products (`test-fixture`); no licensed dataset was ever ingested (see [data-sources.md](data-sources.md)). Most real requests will correctly get "no product meets all of your requirements".
- **USD only, no conversion.** All prices are USD. A budget stated in another currency (e.g. rupees) is reported as not applied, and the user is asked to restate it. Self Search has no exchange-rate data and never converts or guesses.
- **Product types are lexical.** A type is matched by whole words in the title or description, using a curated synonym list. "laptop" also matches *Laptop Stand*. Unlisted types requested through the LLM parser are matched literally. Judgement calls: running shoes count as footwear satisfying "sneakers"; earbuds count as headphones.
- **Offline ranking is weak.** The vector index is 64-d feature hashing (no embedding model installed). Hard constraints stop it from causing *wrong* results, and hash-only matches are dropped when no product type is given, but ranking *within* valid products is modest. Queries using words absent from the catalogue (e.g. "morning run") can return nothing.
- **Exclusions are mention-based.** "without RGB" removes products whose title or description mention RGB. It does not prove the remaining products lack the feature.
- **Refinement wording.** "only 4+ stars" refines the previous search. A bare "under $50" is treated as a new search, without the earlier product type.

## Frontend limits (Phase 9)

- **Browser verification is headless Chrome only (Stage 1).** `scripts/ui_check.py` checks layout, covering, focus, contrast, reduced motion and every control flow at five viewports, including 320 px and 200 % zoom. Not checked:
  - screen-reader output;
  - real touch devices;
  - Firefox and Safari;
  - Windows high-contrast mode.
- **The visual design depends on Streamlit 1.66 internals.** The CSS targets its `data-testid` attributes and `st-key-*` classes, so the version is pinned. A Streamlit upgrade can silently break the styling (not the functionality); re-run `scripts/ui_check.py` before upgrading.
- **The decorative animation costs a little CPU.** In headless Chrome, idle main-thread time was about 4–6 % of one core with the animation and about 2–3 % with reduced motion; Streamlit's own idle work accounts for most of it.
- **The chat input is tall on phones.** At 390 px its two-line placeholder makes the sticky bar about 200 px high. It is Streamlit's component and was left as is.
- **The summary is not validated.** The Phase 6 validator checks claims, not `answer.summary`. The UI labels it "AI-written summary · not individually verified".
- **Every current image URL is a placeholder domain** (`example.com`), so every card shows the neutral "No image available" graphic. Real remote images (with `SHELFSENSE_LOAD_REMOTE_IMAGES=true`) were tested only with a fake client, not against real image hosts.
- **No aspect bars or complaint summaries.** Phase 2 review intelligence is not implemented; the UI says "Aspect analysis not available".
- **Narrow preference detection.** Only capitalised brand names in first-person statements are detected ("I prefer Sony", not "I prefer sony"), plus explicit budgets and catalogue category names. Other phrasings are not suggested. Nothing is ever saved without a click.
- **UI state is per browser tab.** Chat history lives in Streamlit session state and is lost on reload; the server session survives only until the API restarts.
- **Latency.** In the live run, answer turns took about 6–8 s with no model call (fallback mode), above the spec's ~3 s target for showing cards. Not tuned in Phase 9.
- **Cards are a wrapping grid** (approved), not a horizontally scrolling strip.

## Legacy combined index is inconsistent (configuration-safety phase)

`data/vector_store/faiss.index` is used only by the legacy `/ask` endpoint (`src/retrieval/retrieval.py`). A read-only check on 2026-10-21 found:
- 140 vectors but only 100 id-map entries;
- 40 duplicate ids;
- 20 reviews missing.

Products and reviews share small integer ids, and both were once written into this one store.

By decision it was **not modified or rebuilt**. The manifest records it as excluded, with its sha256 and the reason. The API logs a warning about it at startup, and `generate_embeddings.py` never writes it. `/chat` does not use it: `/chat` reads `products.index`, which is covered and verified.

- **Embedder of the current indexes:** not recorded when they were built. The 64-d dimension matches the offline feature-hashing embedder. Builds from now on record their embedder.
- **Dimension mismatch at query time:** fixed on 2026-10-21. The query-time embedder is pinned to the offline 64-d hashing embedder unless `SHELFSENSE_EMBEDDING_BACKEND=ollama` is set explicitly, so a reachable Ollama no longer changes query vectors.

## Planned topics
- Dataset bias and coverage gaps
- Small local model error rates and hallucination
- Review spam and manipulation
- Sentiment analysis accuracy limitations
- Category and language coverage
- Performance on constrained hardware

## Expanded 86,588-product catalogue (2026-10-23)

These points supersede the 50,000-product figures below, which are kept as the record of that catalogue.

**Data**
- **The target was not reached.** The catalogue has 86,588 products, not 100,000: there are only 1,378 new sarees (allocation 8,000) and 210 lehengas (allocation 7,000).
  - That is every non-duplicate saree and lehenga in Amazon Reviews 2023 with a usable review. The other eight categories met their allocations exactly.
  - No other source under a stated licence was available, and nothing was invented to fill the gap (`data-sources.md`).
- **Weaker review evidence for the additions:** an added product needs only **one** review of 20+ characters (v3 needed three). Many new products, sarees and lehengas included, have 1–2 reviews, so AI summaries of them are more often "Not verified" or "insufficient evidence".
- **The source is a US marketplace.** Sarees, lehengas and Indian jewellery are amazon.com listings from 2023, not Indian retail listings. Titles are often keyword-stuffed.
- **Brand and description coverage is low:** 8.0% of products have a brand field and 22.3% a description. Store names are kept as attributes, never as brands.

**Prices**
- 56.0% of products (48,464) have no price at all, including most sarees and lehengas. They show "Price unavailable" and never meet a ₹ budget.
- Converted prices cover 34.1% (29,502), and labelled estimates 10.0% (8,622, footwear and bags only).

**Images**
- 97.33% of products have a validated photo URL. Sarees, lehengas and earrings are each at 99.5% or more.
- Some older v3 shoe types fall below 95% (boots 88.7%, sneakers 90.6%, sandals 90.8%), because their source has no other usable photo.
- URLs were validated once, on 2026-10-23; a URL that later breaks falls back to the second photo, or to "Image unavailable".

**Matching**
- **Strict matching is lexical. These gaps were measured** on held-out queries (`evaluation-results.md`):
  - **"silk saree" accepts art silk.** 4 of 10 results were art-silk sarees, which the evaluation does not count as silk.
  - **Multi-pair earring packs and earring "sets"** can pass the earrings type.
  - **Keyword-stuffed titles pass.** For example, a men's kurta set titled "… Red … Party Dress Suit" matches "red summer dress".
- **"velvet lehenga" finds nothing,** correctly: in the three lehengas that mention velvet, only the blouse is velvet.

**Performance**
- **Searches are slower:** the live search median on 86,588 products was 4.1 s and 5.1 s in two runs of 24 queries (the second right after a restart). On v3 it was 1.4 s, over a different set of 12 queries (2026-10-22). The slowest query took 14.6 s.

## Prices in INR only (2026-10-23)

These points supersede the "Prices and currencies (2026-10-22)" notes below; dollar prices are no longer shown.
- **Converted prices reflect 2023 USD prices at a 2023 rate.** They are amazon.com prices from the 2023 crawl, converted at the ECB rate of 2023-09-29 (₹83.08 per US dollar). They are not current Indian retail prices. The card says "Converted from source price", and the details give the rate.
- **Rounding:** conversion rounds to whole rupees, so a converted price can differ from an exact conversion by up to 50 paise.
- **Unconvertible prices:** any currency without a documented rate would show "Price unavailable". This dataset has only USD.
- **Foreign amounts inside source text** (a review saying "worth $20") are removed from the page and replaced with a visible marker. The stored review text and the evidence the validator checks are unchanged.

## Prices and currencies (2026-10-22)

- **Genuine prices are dollars.** The Amazon US dataset's only real prices are USD (3,451 of 50,000 products). They are shown in dollars, labelled "Source price · USD", and never converted.
- **Rupees are synthetic estimates only:**
  - 7,454 products (footwear and bags) have an illustrative INR estimate.
  - Clothing types (dresses, shirts, jackets, jeans, belts and others) have **no estimate range**, so they can show only a source price or "Price unavailable" (since 2026-10-23 the source price is shown converted to ₹), and without a source price they never meet a ₹ budget.
  - Adding ranges for them would be a new, approved change to the versioned estimate config, not a display fix.
- **Estimates are off unless enabled:** the default is off, as the approved amendment requires, and `scripts\run_demo.ps1` turns them on. An API started another way (for example plain `uvicorn`) shows only source prices and "Price unavailable", which makes the page look dollar-only.

## Product-first search and optional summary (2026-10-22)

- **AI text is opt-in:** a search shows products with no AI-written text. Only "Summarise reviews", questions and comparisons call the model.
- **The summary covers the top 3 results only;** the other cards have no claims. Claim support on summaries was 0.44 in the final run (0.55 when every search described 4 products), and that run is one sample.
- **The slow part is still slow:** a summary takes about 40–80 s on this CPU. It no longer blocks the page, but a GPU is still needed for interactive model answers.
- **Questions and comparisons still block the page** while they run (as before). Only the summary runs in the background.
- **A shorter prompt was measured and rejected:** it was about 6–7 s faster, but it doubled citations of the wrong product's review. Prompt-prefix reuse in Ollama was not observed in the token counts, so no speed-up is claimed from it.

## 50,000-product catalogue and strict matching (2026-10-22)

These points supersede the 20,000-product figures in the two sections below, which are kept as the record of that catalogue.
- **Data:** 50,000 *Amazon_Fashion* products and 271,939 reviews (selection v3), at schema v4 with no profile tables.
- **Prices:**
  - 3,451 products (6.9%) have a USD source price; there are no INR source prices.
  - 7,454 (14.9%) have a synthetic INR estimate (footwear and bags only).
  - The rest have neither, so no dress, shirt or jacket can meet a ₹ budget. "red summer dress under ₹2000" truthfully returns no exact matches.
- **Strict matching trades recall for precision:** type, colour and material must be *stated* by the listing (title, or `details.Color` / `details.Material`).
  - Many listings state no colour. For "black heels", 56 heels state black and 67 heels state no colour, so those 67 are left out (and the page says so).
  - Types are judged on the title only. A product whose title omits its type (for example a pump titled only by model name) is not found by a type query.
- **The rules are hand-written lexical rules.** Precision was measured on 32 queries (20 used for tuning, 12 held out; see `evaluation-results.md`), which is a small sample. Unusual titles can still slip through or be missed.
- **Brand detection** recognises names from the brands table and from stores with at least 3 products. Brands sold under a different store name, or with fewer listings, are not recognised as brands; the words are still searched as keywords.
- **Duplicate listings:** identical titles under different ASINs count once. Near-identical titles (a different colour word, for example) remain separate products.
- **Images:** 49,532 of the 50,000 products have a photo URL; 468 have only Amazon's blank placeholder.

## Experimental Fashion catalogue and synthetic INR estimates (dataset phase 4)

- **Data:** 20,000 Amazon Reviews 2023 *Amazon_Fashion* products and 126,977 reviews (US marketplace, 2023 crawl). The dataset states no licence; local academic use only.
- **Prices:** source prices are **USD only**, on 8.2% of products. There are **no genuine INR prices**.
- **Field coverage:** descriptions 7.1%, brands 1.6%. The source has no category list, so product types come from titles.
- **Synthetic INR estimates are illustrative, not market prices.** They come from hand-written category ranges chosen by title keywords (`config/price_estimate_ranges.toml`).
  - **Keyword matching misfires:** accessories whose titles mention another product get that product's range. For example, lapel pins "for backpacks and jackets" get the backpack range, and heated vests mentioning "power bank" get the charger range.
  - **Coverage:** only footwear, bags and electronics have ranges, so apparel (shirts, dresses, jackets, jeans) has no estimate. 6,454 of the 20,000 products have one.
- **Product types were broad (fixed 2026-10-22 by strict matching):** "sneakers" used to map to the broad `footwear` type, so sandals and boots also matched a sneakers query.
- **The legacy `/ask` endpoint** calls Ollama directly, with its own model setting (`LLM_MODEL`, default `llama3`, not installed) and no evidence validation. On the Fashion catalogue there is no legacy `faiss.index`, so it has no evidence either. No client uses it; the UI uses `/chat`. It was left unchanged for backward compatibility, and it should not be used in demos.
- **Ollama:** installed since 2026-10-21 (`llama3.2:3b`, CPU-only), so the LLM-written path is now exercised. Latency is covered in "Final demo" below.
- **The validator still does not extract ₹ amounts** (unchanged). Estimates never reach the model, so it has no estimate to repeat.

## Final demo (2026-10-22)

- **CPU latency:** a model-written answer on the Fashion catalogue takes about 40–110 s with `llama3.2:3b` on this CPU (median 98.6 s for searches; measured in `evaluation-results.md`).
  - The 110 s per-message budget keeps every reply inside the UI's 120 s.
  - When the budget runs out (1 of 14 representative turns), the answer is the labelled, evidence-based fallback.
  - Interactive speed (a few seconds) needs a GPU. `llama3.2:1b` was faster but measurably less grounded, so it is not the default.
- **Claim support is moderate:** on Fashion, model answers had a mean claim support rate of 0.64 (0.45 on the off-catalogue eval set). The reviews are short and informal, and the validator is deliberately strict.
  - Unsupported, cross-product and numeric-mismatch claims are flagged in the answer, never hidden.
  - No fabricated citations were observed.
- **Relevance is lexical:** with a 64-d feature-hashing embedder, ranking is driven by keywords.
  - Products containing all of the user's words come first, but a word can match the wrong kind of product. For example, "summer dress under ₹2000" returns sandals titled "… Sandals Summer Dress Flats", because only those have INR estimates. Real dresses have no price or estimate and are correctly excluded.
  - The curated product types covered footwear, bags and electronics, not apparel. Since 2026-10-22, strict types cover apparel too (dresses, skirts, jeans, shirts, jackets and more).
- **Price coverage:**
  - 1,648 products (8.2%) have a source price, in USD only.
  - 6,454 have a synthetic INR estimate (footwear and bag ranges).
  - 11,898 have neither, so they never meet a budget.
  - An INR budget therefore applies only to estimated products, and a USD budget only to USD-priced ones.
- **The 20,000-product Fashion database keeps the obsolete profile tables** (the 50,000-product v3 database has none): it is at schema v3 and still holds the one profile row created during testing, which the instructions say to keep. No code reads them. Migrating it to v4 would be refused by the guard until that row is exported or removed.
- **The legacy `/ask` endpoint** is unchanged and unused (see above); it is not evidence-validated.
- **The UI must be restarted after code changes** (Streamlit keeps imported modules in memory); `scripts/run_demo.ps1` starts both services fresh.

## Product images (2026-10-22)

- **Images need internet.** Photo URLs (19,826 of 20,000 Fashion products) point to Amazon's image host and are fetched at display time by the UI server. Offline, or with `-NoImages`, cards show a neutral "No image available" placeholder. Search and answers work the same either way.
- **174 products have no photo.** The source has only Amazon's blank placeholder image for them, and it is skipped.
- **Images are illustrative.** They show the source listing's main image, which may show one colour or size of a multi-variant listing. They are not verified against the title, and they are never used as evidence.
- **Rights:** the images belong to their owners, and the dataset states no licence. They are used only in the local academic demo; nothing is stored or redistributed (see `data-sources.md`).
