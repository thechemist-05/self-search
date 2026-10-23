# Self Search RAG Design (Phase 4)

This document specifies the retrieval architecture, search algorithms, metadata filtering, rank fusion, and evidence packaging implemented in Self Search Phase 4.

---

## 1. Overview and Core Philosophy

In Self Search, the Language Model (LLM) is **not** the search engine and **not** the source of factual product truth. All products and factual claims must originate from retrieved, verified database records and review snippets.

Phase 4 implements a flexible, multi-stage retrieval pipeline with four selectable modes:
1. **`keyword`**: BM25 retrieval over SQLite FTS5.
2. **`semantic`**: Dense vector retrieval over FAISS indexes.
3. **`hybrid`**: Reciprocal Rank Fusion (RRF) combining keyword and semantic candidate lists.
4. **`reranked_hybrid`**: Cross-Encoder reranking applied to top hybrid candidates.

---

## 2. Retrieval Components

### 2.1 SQLite FTS5 Keyword Retrieval (BM25)
- **Virtual Table**: `products_fts` indexing `title`, `description`, `attributes_json`, synchronised automatically with `products` via triggers.
- **Query Sanitisation**: Tokens are extracted, quoted, and safely disjoined with `OR` (`"fast" OR "charger"`), avoiding FTS5 syntax errors while rewarding documents matching multiple tokens.
- **Scoring**: SQLite FTS5 `bm25(products_fts)` is inverted to positive values so higher scores represent higher relevance.

### 2.2 Semantic Retrieval (FAISS)
- **Vector Store**: Two separate persistent FAISS indexes:
  - `data/vector_store/products.index`: dense embeddings of product title + description.
  - `data/vector_store/reviews.index`: dense embeddings of review chunks.
  - The directory is set by `SHELFSENSE_VECTOR_STORE_DIR` (default `data/vector_store`). The older per-file variables `PRODUCT_INDEX_PATH`, `REVIEW_INDEX_PATH` and `VECTOR_STORE_PATH` still override it.
  - `manifest.json` records a fingerprint of the catalogue the indexes were built from, plus each index file's sha256 (see §7).
- **ID Preservation**: Uses `faiss.IndexIDMap2` to ensure SQLite integer IDs map 1-to-1 to vector IDs.
- **Distance Normalisation**: Similarity score is computed as `1.0 / (1.0 + L2_distance)`.

### 2.3 Metadata Filtering
- **Class**: `MetadataFilter`
- **Supported Fields**: `min_price`, `max_price`, `category` (or `category_id`), `brand` (or `brand_id`), `min_rating`.
- **Dual Execution**:
  - Compiles into SQL `WHERE` clauses for database-level filtering.
  - In-memory `matches(product_dict)` for candidate re-filtering after vector search.

### 2.4 Reciprocal Rank Fusion (RRF)
Combines candidate rankings from semantic and keyword retrievers:

$$RRF(d) = \sum_{m \in M} \frac{w_m}{k + r_m(d)}$$

- Default smoothing constant $k = 60$.
- $r_m(d)$ is the 1-based rank of item $d$ in system $m$.
- Preserves full item metadata and source rank attribution (`source_ranks: {'keyword': 1, 'semantic': 1}`).
- **Lists fused (2026-10-22):**
  - `keyword`: any query word, plus the requested product type's synonyms;
  - `keyword_own_words`: every word the user typed (FTS5 `AND`);
  - `semantic`.
- **Relevance floor:** a product must match the keyword channel. The offline index is feature-hashed, not semantic, so semantic-only hits are dropped. Since 2026-10-22 this also applies when a product type is set.
- **Tiering:** products containing all of the user's words come first, in fused order. Synonym-heavy titles therefore no longer outrank exact matches. For example, "Knee High Boots … Winter Shoes" used to be the top result for "running shoes"; now actual running shoes are. Type, price and rating constraints apply to both tiers.

### 2.5 Optional Cross-Encoder Reranker
- Model: `cross-encoder/ms-marco-MiniLM-L-6-v2`.
- Formulates query-document pairs: `(query, f"{title}. {description}")`.
- Configurable via `SHELFSENSE_ENABLE_CROSS_ENCODER` or runtime flag; falls back cleanly to hybrid ranks in offline environments without pre-downloaded weights.

### 2.6 Review Evidence Retrieval & Token Budgeting
- **Function**: `assemble_evidence_pack(conn, product_ids, query, max_reviews_per_product, token_budget, max_words)`
- **Ordering:** with a query, each product's reviews are ordered by how many query terms they mention, then helpful votes, then rating. Near-duplicate texts (same first 12 words) are skipped. Without a query the order is helpful votes, then rating.
- Assigns stable citation identifiers: `[R1]`, `[R2]`, `[R3]`, etc. Snippet text is copied verbatim from the database (truncated to `max_words`).
- Enforces a strict token budget (estimated at about 4 characters per token).
- Outputs structured `EvidencePack` ready for LLM prompt injection and citation validation.
- **Answer budget used by the agent (2026-10-22):**
  - A search retrieves and shows 10 products **without calling the model** (product-first). Its evidence pack covers the top products and is shown under "Reviews and details".
  - The optional summary (`POST /chat/summary`) has the model write about the **top 3** only. They are the only products with evidence: at most 2 reviews each, about 60 words per snippet, 800 tokens in total. (It was the top 4 while every search called the model.)
  - The validator checks the answer against that same pack.
  - Compare uses at most 4 products with the same per-product limit. A product question uses up to 4 reviews of one product.
  - **Prompt layout:** the fixed instructions (`SYSTEM_PROMPT`) come first and never vary; the variable parts follow (query, constraints, products, evidence). Ollama's token counts showed no prefix reuse between calls, so no speed-up is claimed from it.
  - **Prompt text unchanged:** a trimmed wording was measured on 2026-10-22 and rejected. It saved about 15% of prompt tokens but roughly doubled cross-product citations (see `evaluation-results.md`).
  - On the Fashion catalogue the first budget cut the answer prompt from about 11,000 to about 5,000 characters; the 3-product summary prompt is about 4,300 characters (see `evaluation-results.md`).

---

## 3. Evaluation Benchmark & Results

Evaluated on `eval/queries.jsonl` (20 curated, multi-category shopping queries against the fixture catalogue):

| Retrieval Mode | Recall@1 | Recall@3 | Recall@5 | MRR |
|---|---|---|---|---|
| **KEYWORD** | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| **SEMANTIC** | 0.8000 | 1.0000 | 1.0000 | 0.8917 |
| **HYBRID** | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| **RERANKED_HYBRID** | 1.0000 | 1.0000 | 1.0000 | 1.0000 |

Detailed JSON metrics are persisted in `eval/results/retrieval_evaluation.json`.

---

## 4. CLI Execution

The CLI script `scripts/run_retrieval.py` allows interactive multi-mode retrieval:

```bash
# Keyword search with price filter
python scripts/run_retrieval.py --query "fast charger" --mode keyword --max-price 30

# Semantic vector search
python scripts/run_retrieval.py --query "comfortable wireless earbuds" --mode semantic --limit 5

# Hybrid RRF with review evidence packaging
python scripts/run_retrieval.py --query "waterproof backpack" --mode hybrid --with-evidence --budget 1500
```

---

## 5. Version 4: Personalised RAG (removed 2026-10-21)

Personalised re-ranking (`src/personal/`) and the `PERSONAL` intent ("something like what I saved") were removed together with user profiles. Every search now uses the same `HybridRetriever.retrieve_with_evidence(...)` path, so ranking is identical for all users. Variant D of the Phase 10 evaluation plan (personal RAG) no longer applies.

---

## 6. Hard Constraints and Relevance (Phase 9 relevance fix)

**Problem observed.** "I need a shoe under 4000 rupees, which can be everyday use, sneakers." returned a power bank, a laptop stand, the USD 89.99 running shoes and more electronics. Root causes:
1. Nothing represented "must be a shoe". The rule-based parser never set a category, and stored categories are too coarse.
2. The keyword query kept stopwords: "a" matched the "A" of "USB-A", making the power bank the only keyword hit. "shoe" did not match "Shoes".
3. The offline "semantic" channel is 64-d feature hashing, not semantics, and RRF always filled 10 slots.
4. "rupees" was discarded, so 4000 was compared directly against USD prices and excluded nothing.

**Design now.** Hard constraints are enforced *inside* retrieval, before ranking, in every mode.

| Constraint | Source | Enforced against |
|---|---|---|
| Product type (`MetadataFilter.product_types`) | Curated synonym list in `src/retrieval/constraints.py` (e.g. footwear = shoe, sneaker, trainer, boot, sandal, …), detected from the user's words, or from the LLM parser's `category` | Whole words in `title` / `description`, plural-insensitive |
| Category | Only when the request names an existing catalogue category exactly | `categories.name` |
| Price | Parsed amount and **stated currency** (₹ / Rs / rupees / INR, $ / USD / dollars, €, £) | `price` **and** `currency`. Only same-currency products can satisfy it. Never converted |
| Rating, brand | Parser / LLM | `avg_rating`, `brands.name` |
| Exclusions | "not KeyCraft" (a catalogue brand) / "without RGB" (a term) | Brand name / whole words in title or description |

- `MetadataFilter.matches()` checks everything. Keyword search over-fetches (500 rows) and then filters. Semantic search scans the whole index when text constraints exist. So neither channel, nor RRF or reranking, can return a violating product. This is verified by a seeded property test over 240 constraint sets × 4 modes.
- **Currency.** If the budget's currency is not used by the catalogue, the agent returns a clarification. No retrieval or generation runs, nothing is converted, and the message says how many products meet the other requirements. If no currency is stated, the catalogue's single currency is assumed **and shown** ("USD 4,000.00 (no currency given; catalogue prices are in this currency)"). If the catalogue had several currencies, the agent would ask.
- **Keyword query.** Stopwords, tokens under 3 characters and bare numbers are dropped. Words of 4+ characters become plural-insensitive prefixes (`"shoe"*`). Requested product-type synonyms are added to the keyword query (ranking only).
- **Relevance floor.** Without a product-type constraint, hybrid results must match the query's words. Hash-similarity-only hits are dropped (`HybridRetriever(semantic_only_results=False)`), so an unknown product ("coffee maker") gives an honest no-match instead of unrelated items. Enable `semantic_only_results` once a real embedding model indexes the catalogue.
- **Reporting.** `AgentResponse.constraints` lists each interpreted constraint (applied, or not applied with a reason) and how many catalogue products satisfy all applied ones. Each card's `constraint_checks` cites the database fields that satisfy them.

**Phase 4 benchmark after the change** (`eval/queries.jsonl`, k = 5): unchanged. Keyword, hybrid and reranked are all 1.0; semantic has Recall@1 0.80 and MRR 0.8917. That benchmark's queries reuse title words, so it does not exercise these failure modes; the new regression tests do.

**Limits.** Matching is lexical: "laptop" also matches *Laptop Stand*. The type list is a curated judgement (e.g. running shoes count as footwear satisfying "sneakers"; earbuds count as headphones). Ranking *within* valid products is still weak offline.

## 6a. Strict product matching (2026-10-22)

A request's product, colour, material, brand, budget and rating are **hard constraints applied before ranking**. Similarity can find candidates, but it can never qualify a product on its own. Results are never padded: three exact matches show three cards, and none shows an empty state with explicit "broaden the search" options.

- **Product type** (`STRICT_TYPES` in `src/retrieval/constraints.py`): checked against the **title only**, with rules per type.
  - **Required phrases.** Running shoes need "running" or "jogging" plus a shoe noun.
  - **Conflicting types veto.** Football, cleat or sandal rules out running shoes; boots or sneakers rule out heels.
  - **Accessories that name the type veto.** Cleaners, shoelaces, insoles, charms, "for backpack" items.
  - **Masks remove look-alike phrases.** "short sleeve" isn't shorts, "dress shirt" isn't a dress, "cap sleeve" isn't a hat.
  - **Shorts are matched literally,** so the adjective "short" never counts.
  - The original electronics types keep the old title-or-description synonym match.
- **Colour and material:** stated by the listing's structured `details.Color` / `details.Material`, else by its title. Anything else counts as **unknown, never assumed**.
  - Colour families: "blue" accepts navy, but "navy" accepts only navy, and "burgundy" is a red.
  - Faux, vegan or PU leather is never leather.
  - Brand names and phrases such as "Red Wing" or "Black Friday" are masked out.
  - Products left out for an unknown colour or material are counted (`unknown_attribute_products`), so the UI can explain why it shows nothing.
- **Brand:** detected deterministically, with no model call. A query matches a name in the brands table, or a store name used by at least 3 products, unless the name is an ordinary word. A product meets the brand if its brand field, store or title starts with that name as whole words.
- **Recall:** keyword retrieval over-fetches 2,000 rows before the Python-only constraints apply. Hybrid fusion still ranks the remaining exact matches, products containing all the user's words first.
- **Evidence:** unchanged. The model sees only the top exact matches with their own reviews, and the validator rejects cross-product citations and numeric mismatches.
- **Measured:** see `evaluation-results.md` ("Strict matching"). Tests are in `tests/unit/test_strict_types.py` and `tests/integration/test_strict_matching.py`.

## 6b. Sarees, lehengas, earrings and occasions (2026-10-23)

- **Strict types with synonyms** (`STRICT_TYPES`, `PRODUCT_TYPES` in `src/retrieval/constraints.py`), judged on the title only, like the other Fashion types. A category word that appears only in a review never makes a product that type.
  - **saree:** "saree", "sari", "saaree" (plurals included). Saree blouses, petticoats, shapewear, pins, covers, bags and anything "for saree" are masked or vetoed. So are lehengas, kurtas and jewellery that mention sarees.
  - **lehenga:** "lehenga", "lehnga", "lengha", "lehanga", "lehenga choli", "ghagra (choli)", "chaniya choli". Can-can petticoats, covers and bags are vetoed. A "lehenga saree" counts as neither type: the title names both.
  - **earrings:** "earring", "jhumka/jhumki", "chandbali", "ear stud", "ear cuff", "huggie". Necklaces, pendants, bracelets, bangles, rings, maang tikkas, sets, and earring backs, holders and findings are vetoed. An earring search never returns them; the catalogue does not hold sets.
- **Materials and work** (strict, stated by `details.Material` or the title): georgette, chiffon, organza, crepe, net, Banarasi, Kanjivaram/Kanchipuram, kundan, meenakari, pearl, oxidised silver, gold plated and sterling silver, besides silk, cotton, velvet and the earlier ones.
- **Occasions** (new strict attribute `occasions`): bridal, wedding (which includes bridal), festive (Diwali, Navratri, Eid, puja …) and party.
  - The listing must state the occasion in `details.Occasion`, the title or the description; unstated means unknown, never assumed.
  - Products left out for an unstated occasion are counted (`unknown_attribute_products`), and "Broaden the search" offers *Any occasion*.
  - "Everyday" or "casual" describe how something is worn, are rarely stated, and stay ordinary search text.
- **Budgets** are INR and apply to each product's normalised INR price (§ prices in `api-documentation.md`), so "red silk saree under ₹3,000" requires: saree, red, silk, and ≤ ₹3,000.
- **Tests:** `tests/integration/test_ethnic_categories.py` (an adversarial fixture through the real pipeline) and `tests/unit/test_strict_types.py`. Evaluation queries: `eval/strict_queries_v4.jsonl`, written before the expanded catalogue was built.

## 7. Index manifest and startup check (configuration-safety phase)

FAISS ids are SQLite row ids, so indexes built from one database return wrong products when served against another. The manifest (`src/vector_store/manifest.py`) makes that mismatch fail loudly.

- **Fingerprint:**
  - row counts and a sha256 over each product's `id, source_id, external_id, title, description`;
  - the same over each review's `id, product_id, source_id, external_id, title, text`.

  Personal tables (users, preferences, wishlist, feedback) are excluded, so profile activity never invalidates the indexes.
- **Covered indexes:** `products.index` and `reviews.index`, with their `.pkl` id maps. The manifest records each file's sha256, vector count and dimension, plus the embedder name.
- **Consistency check** (run before any manifest is written):
  - vectors equal id-map entries;
  - no duplicate ids;
  - every indexed id exists in the database;
  - every row is indexed;
  - the entity types are right;
  - all covered indexes share one dimension.
- **Startup:** the API builds its controller at startup and stops (uvicorn exits) if the manifest is missing, unreadable, or out of date. A controller injected by tests skips the check.
- **Building:** `scripts/generate_embeddings.py`:
  - reads `SHELFSENSE_DB_PATH`, read-only;
  - picks one embedder for the whole build;
  - builds into a temporary directory and validates;
  - then replaces the active files, indexes first and manifest last;
  - restores the previous files if anything fails.

  `/populate` uses the same rebuild. Before this phase both appended to existing indexes, which duplicated vectors.
- **Existing indexes:** `scripts/verify_index_manifest.py --check` only reports. `--write` adds `manifest.json` only after the check passes.
- **Legacy `faiss.index`:** read only by `/ask`. It is **excluded**: its checksum is recorded with the reason, a warning is logged at startup, and it is never written by the build. See `docs/limitations.md`.

