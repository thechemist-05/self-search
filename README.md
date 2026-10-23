# Self Search

> *Tell it what you want. It shows you the products, and proves why.*

Self Search is a conversational product discovery assistant. You describe what you want in plain language, for example "red silk saree under ₹3,000", "bridal jhumkas under ₹1,500" or "men's leather belt". It returns real products from its catalogue that meet every stated requirement. On request it summarises what customers say about them, and every statement it makes points back to the review or database field it came from.

It is an academic research prototype built with **LLM + RAG + GenAI**. It runs entirely on one machine, with no paid APIs or cloud services, and it is not affiliated with any retailer.

**Current version (2026-10-23):**
- **Catalogue:** 86,588 products and 531,796 customer reviews from Amazon Reviews 2023.
- **Prices:** shown in Indian rupees only.
- **Tests:** 1,024 automated tests pass.

---

## Contents

1. [The business view](#1-the-business-view)
2. [What a user can do](#2-what-a-user-can-do)
3. [How it works, end to end](#3-how-it-works-end-to-end)
4. [Models used](#4-models-used)
5. [Tech stack](#5-tech-stack)
6. [API reference](#6-api-reference)
7. [Data: catalogue, prices and images](#7-data-catalogue-prices-and-images)
8. [Trust and safety rules](#8-trust-and-safety-rules)
9. [Implementation plan, start to finish](#9-implementation-plan-start-to-finish)
10. [Quality: tests and measured results](#10-quality-tests-and-measured-results)
11. [Running it](#11-running-it)
12. [Project structure](#12-project-structure)
13. [Known limitations](#13-known-limitations)
14. [Documentation](#14-documentation)
15. [Summary](#15-summary)

---

## 1. The business view

### The problem
Shopping search breaks down in three ways that cost sales and trust:

| Problem | What the shopper experiences |
|---|---|
| **Keyword search ignores requirements** | Searching "black heels" also returns white heels, heeled boots and heel grips. The shopper has to filter by hand. |
| **AI shopping assistants invent things** | Chatbots state prices, features or "customers love…" claims that no source supports. One wrong claim discredits the whole answer. |
| **Reviews are too many to read** | A product's value lives in its reviews, but nobody reads hundreds of them before deciding. |

### The proposition
Self Search combines strict, explainable product matching with a local language model that may only say what the evidence supports:
- **Precise results:** a product is shown only if its listing *states* the requested type, colour, material, occasion, brand, rating and budget. Products whose listing leaves a requirement unstated are left out, and the page says how many.
- **Answers you can audit:**
  - every AI claim cites review IDs;
  - every claim is checked against those reviews, and every number against the database;
  - unsupported claims are visibly marked "Not verified".
- **Honest gaps:** when nothing matches, or the evidence is thin, it says so instead of padding results or guessing.
- **Private and low-cost:** it runs offline on a laptop, sends no user data anywhere, and needs no paid services.

### Who it is for
- **Shoppers:** they get fewer and correct results, with reasons they can check.
- **Retailers and marketplaces (as a reference design):** a pattern for grounded AI shopping assistants that do not hallucinate prices or features.
- **Researchers and students:** a complete, measured example of retrieval-augmented generation (RAG) with evidence validation.

### Market focus
The current catalogue targets an Indian shopper:
- **Prices:** all in ₹, with Indian digit grouping such as ₹1,25,000.
- **Ethnic wear and jewellery:** sarees, lehengas, jhumkas, chandbalis, maang tikkas and juttis, with Indian spellings (sari, lehnga, jhumki).
- **Occasions:** bridal, wedding, festive and party.

---

## 2. What a user can do

| Action | Example | What happens |
|---|---|---|
| **Search** | "running shoes under ₹3000" | Cards appear in a few seconds. Filter chips show what was enforced: `Running shoes`, `Under ₹3,000 ✕`. No model call is needed. |
| **Refine** | "only black ones" or "under ₹2000" | The conversation keeps the earlier filters and applies the change. ✕ on a chip removes that filter. |
| **Summarise reviews** (optional) | Click **Summarise reviews** | The local model summarises the top 3 results in the background. Each claim is marked ✅ *Backed by reviews* or ⚠️ *Not verified*. |
| **Ask about a product** | "Ask about this", then "is the fabric good?" | The answer uses only that product's own reviews; if they don't say, it reports insufficient evidence. |
| **Compare** | Tick two cards, then "Compare selected" | A side-by-side table built from database values, plus a short comparison citing each product's reviews. |
| **See the evidence** | "Reviews and details" on a card | The cited review snippets, word for word, plus the price explanation and why the product matched. |
| **Broaden** | After "No exact matches" | Options such as *Any colour* or *Any price* run as a separate new search; near-misses are never mixed into exact results. |

Questions outside shopping ("what is the weather?") and prompt-injection attempts get a short refusal, with no products and no model call.

---

## 3. How it works, end to end

```
 Browser ──► Streamlit UI (port 8502) ──HTTP/JSON──► FastAPI backend (port 8000)
                                                        │
                    ┌───────────────────────────────────┤
                    ▼                                   ▼
          1. Intent router                    Session store (filters,
          2. Query parser (rules first)        products shown, history)
          3. Constraint resolver
                    │
                    ▼
          4. Hybrid retrieval ─────► SQLite FTS5 (BM25 keyword search)
             + strict filters ─────► FAISS (vector search, product + review indexes)
             + Reciprocal Rank Fusion
                    │
                    ▼
          5. Product cards (database values only, prices normalised to ₹)
                    │   (optional: Summarise / Ask / Compare)
                    ▼
          6. Evidence pack (top reviews per product, with IDs)
          7. Local LLM: Ollama llama3.2:3b → JSON answer citing evidence IDs
          8. Evidence validator: citations, review support, numeric recheck
                    │
                    ▼
          9. Response: cards + validated claims + support report
```

### Step by step
1. **Intent routing** (`src/agent/intent_router.py`). The message is classified as one of: `PRODUCT_SEARCH`, `REFINE_SEARCH`, `PRODUCT_QUESTION`, `COMPARE`, `CLARIFY`, `SMALLTALK` or `OUT_OF_SCOPE`.
2. **Query parsing** (`src/agent/query_parser.py`, mode `rules_first`). Deterministic rules extract:
   - product type, colour, material, occasion and brand;
   - budget, which must be in ₹;
   - minimum rating.

   The model is consulted only for brand cues the rules cannot settle, which saves a model call on most turns.
3. **Constraint resolution** (`src/agent/constraints.py`, `src/retrieval/constraints.py`).
   - Each requirement becomes a hard filter, with a vocabulary of synonyms. For example, "lehnga" means a lehenga, and "leather" means genuine leather, not PU.
   - A budget in another currency gets a request to restate it in ₹.
4. **Hybrid retrieval** (`src/retrieval/`).
   - **Search:** keyword search (SQLite FTS5, BM25) and vector search (FAISS) both run. Their rankings are merged with Reciprocal Rank Fusion (k = 60).
   - **Strict filtering:** a product passes only if its title or structured fields state every requirement.
   - **Budgets:** compared in SQL against prices normalised to ₹.
   - An optional cross-encoder reranker is available in `reranked_hybrid` mode.
5. **Presentation** (`src/agent/presentation.py`). Cards carry only database values:
   - title, photo, rating and review count;
   - price in ₹ with its status: converted, estimated or unavailable;
   - a "why it matched" explanation.
6. **Evidence pack** (`src/evidence/evidence_pack.py`). For a summary, question or comparison, the most useful reviews of the chosen products are collected, each with a stable ID ([R1], [R2] …). The pack is kept within a token budget.
7. **Generation** (`src/llm/`).
   - The prompt holds only the evidence pack, normalised ₹ prices and the user's request.
   - The model must return JSON matching a schema, with every claim citing evidence IDs.
   - Malformed output is retried within a time budget (110 s per message). If the budget runs out, a clearly labelled evidence-based fallback answer is used.
8. **Evidence validation** (`src/evidence/validator.py`).
   - **Citations:** each cited ID must exist and belong to the right product.
   - **Support:** the claim must be lexically and semantically supported by the cited text.
   - **Numbers:** every number (price, rating, count) is recomputed from the database.
   - **Foreign currency:** any amount in a foreign currency is rejected and redacted.
   - Each claim gets a status (`SUPPORTED`, `UNSUPPORTED_LOW_SIMILARITY`, `NUMERIC_MISMATCH` …), and the answer gets a support report.
9. **Response** (`src/agent/schemas.py`). One JSON contract carries cards, constraints, claims, the support report and any clarification question. The UI renders it.

---

## 4. Models used

All models run locally. None is a paid service.

| Model | Role | Used by default? | Where |
|---|---|---|---|
| **Llama 3.2 3B Instruct** (`llama3.2:3b`) via **Ollama** | Review summaries, product Q&A and comparisons, as structured JSON with evidence IDs. Also parses queries when the rules cannot (brand cues). | **Yes** | `src/llm/ollama.py` |
| **Offline feature-hashing embedder** (`offline-feature-hash-v1`, 64 dimensions) | Vectors for the FAISS product and review indexes and for queries. It is deterministic, needs no download and works fully offline. | **Yes** (the current indexes are built with it) | `src/embeddings/embeddings.py` |
| Ollama embeddings | Alternative embedding backend (`SHELFSENSE_EMBEDDING_BACKEND=ollama`) | No, opt-in. The indexes must be rebuilt to use it. | `src/embeddings/embeddings.py` |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` (sentence-transformers) | Optional reranking of fused candidates | No (`retrieval_mode=reranked_hybrid`) | `src/retrieval/rerank.py` |
| Rule-based NLP | Intent routing, query parsing, type, colour, material and occasion vocabularies | Yes | `src/agent/`, `src/retrieval/constraints.py` |

**Why a 3B model:** it runs on a CPU-only laptop. `llama3.2:1b` was measured and rejected for weaker grounding (see `docs/evaluation-results.md`). A two-prompt A/B test kept the original, longer prompt: it was slower but cited the wrong product's review half as often.

**What the model never does:**
- It never sets a price, rating or product fact.
- It never sees synthetic price estimates or original foreign-currency amounts.
- It never decides which products match.

---

## 5. Tech stack

| Layer | Technology | Purpose |
|---|---|---|
| Language | Python 3.11+ (developed on 3.14) | Everything |
| Backend API | **FastAPI** + Uvicorn, Pydantic v2 | REST API, request and response validation |
| Frontend | **Streamlit** 1.66 | Chat UI with product cards, evidence panels and comparison |
| Database | **SQLite** with **FTS5** | Products, reviews, provenance, price normalisation; full-text BM25 search |
| Vector store | **FAISS** (`faiss-cpu`) | Product and review similarity indexes, with a manifest check |
| LLM runtime | **Ollama** | Serves `llama3.2:3b` locally on `localhost:11434` |
| Configuration | pydantic-settings, `.env`, TOML files | Settings, exchange rate, estimate ranges, dataset selection |
| Logging | structlog | Structured JSON or console logs, including latency |
| Data | Hugging Face `datasets` / direct download, tqdm | Fetching and checksumming Amazon Reviews 2023 files |
| Testing | pytest, pytest-cov | Unit and integration tests |
| Code quality | ruff, black | Linting and formatting |
| UI checks | Headless Chrome driven by `scripts/ui_check.py` | Screenshots at 5 viewports, accessibility and image checks |

---

## 6. API reference

Base URL `http://127.0.0.1:8000`; the API binds to localhost only. An optional API key can be set; clients then send it in the `X-API-Key` header. Full details are in `docs/api-documentation.md`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/chat` | Main conversational endpoint. Body: `{"message": "...", "session_id": optional, "product_ids": optional}`. Returns intent, product cards, applied constraints, validated answer (if any), support report and session ID. |
| `POST` | `/chat/summary` | Optional AI review summary of the current results (top 3). Results are cached per session, so a repeat costs nothing. |
| `GET` | `/sessions/{session_id}` | Session state: active filters and products shown |
| `DELETE` | `/sessions/{session_id}` | End a session |
| `DELETE` | `/sessions/{session_id}/filters/{name}` | Remove one active filter (the ✕ on a chip) |
| `GET` | `/products/{product_id}` | One product with its ₹ price status, conversion provenance and reviews |
| `GET` | `/health` | API, database and Ollama status |
| `POST` | `/ask` | Legacy single-shot answer endpoint, kept for compatibility |
| `POST` | `/populate` | Rebuild the vector indexes from the database. The new indexes are validated and swapped in atomically. |

**Key response fields on a product card:**
- `price` and `currency`: always ₹ or null;
- `price_status`: `verified`, `converted`, `estimated` or `unknown`;
- `price_conversion`: the rate, date and source behind a converted price;
- `estimated_price`: always labelled "not a retailer price";
- `image_url` and `image_alternates`;
- `catalog_rating` and `catalog_rating_count`.

---

## 7. Data: catalogue, prices and images

### Source
**Amazon Reviews 2023** (McAuley Lab, UC San Diego; Hou et al., 2024, arXiv:2403.03952).
- **Category files:** *Amazon_Fashion*, *Clothing_Shoes_and_Jewelry*, *All_Beauty*, *Handmade_Products* and *Sports_and_Outdoors*.
- **Verification:** every raw file is verified by sha256 before use.
- **Licence:** the dataset states none, so the catalogue is used for **local academic work only**, with attribution, and raw files are never redistributed.

### Current catalogue: `fashion-v4-20261023T014937Z`

| | |
|---|---|
| Products / reviews | **86,588 / 531,796** (1–8 reviews per product; 96.8% verified purchases) |
| Built from | The 50,000-product v3 Fashion catalogue (kept unchanged) plus 36,588 new products |
| New categories | Sarees 1,378 · lehengas 210 · earrings 6,000 · other jewellery 4,000 · men's fashion 5,000 · footwear 7,000 · bags & accessories 5,000 · beauty 4,000 · home 3,000 · sports 1,000 |
| Target | 100,000. Not reached: the source has no more qualifying sarees or lehengas, and **nothing was invented** to fill the gap |
| Deduplication | By identical ID, identical title, or near-identical title from the same store. 995 candidates duplicated the existing catalogue and 114,381 duplicated each other. |
| Integrity | 0 orphan reviews, 0 duplicate IDs, 0 invalid prices; `integrity_check` ok |
| Traceability | `product_provenance` (source file and line), `catalogue_types`, `price_normalization` and a `dataset_manifest.json` with checksums and counts |
| Indexes | FAISS: 86,588 product vectors and 531,796 review vectors. A manifest ties them to this exact database, and the app refuses to start on a mismatch. |

Earlier catalogues (v3, 50,000 products; v2, 20,000) are kept unchanged and can still be served.

### Prices: rupees only
The source prices are 2023 amazon.com prices in US dollars. Self Search shows **every price in ₹**:

| Price status | Products | Shown as |
|---|---:|---|
| Converted source price | 29,502 (34.1%) | `₹2,492` with the chip "Converted from source price". The rate is ₹83.08 per US dollar (European Central Bank reference rate, 2023-09-29), recorded in `config/exchange_rates.toml`. |
| Synthetic estimate (footwear and bags, no source price) | 8,622 (10.0%) | `≈ ₹1,480` with the chip "Estimated price · Not a retailer price" |
| No price | 48,464 (56.0%) | "Price unavailable". These products never meet a budget. |

- **Conversion rules:**
  - Conversion uses only the documented, versioned rate. The original amount and currency stay in the database for traceability and are never shown.
  - "$" is never simply swapped for "₹". A currency without a documented rate shows "Price unavailable".
- **Estimates:** illustrative category ranges, applied only to unpriced products. They are never sent to the model or used as evidence, and are off by default (the demo script turns them on).

### Images
Photo URLs come from each product's own source record. All 149,224 candidate URLs were checked over HTTP. A URL is kept only if it returns a real image (JPEG, PNG or WebP of at least 1.5 KB), and URLs shared by unrelated products are rejected as stock or placeholder images.
- **Coverage:** **97.33%** of products have a photo. Sarees are at 99.9%, lehengas 99.5% and earrings 99.98%.
- **Fallback:** the card falls back to a second validated photo, and otherwise shows "Image unavailable".
- **No local copies:** images are linked, never downloaded or stored.

---

## 8. Trust and safety rules

These rules are enforced in code and covered by tests:
1. **No fabrication.** Products, prices, reviews, ratings, brands, specs, images and citations come only from the dataset. The sole exception is clearly labelled synthetic price estimates.
2. **Every AI claim cites evidence IDs**, and is validated. Unsupported claims are flagged and never shown as fact.
3. **Insufficient evidence is said out loud,** for example "The reviews don't say".
4. **Database values only on cards.** The model cannot change a price or rating.
5. **Strict matching over padding.** "No exact matches" is a valid answer, and alternatives are shown only when the user asks to broaden.
6. **Rupees only,** with documented conversion provenance.
7. **Untrusted text is escaped.** Review text cannot inject markdown, links or LaTeX into the page.
8. **Privacy.**
   - Everything runs on localhost; no user profiles or personal data are stored, and reviewer IDs and names are dropped at ingestion.
   - Sessions live in memory only.
9. **No scraping.** Only published research datasets with documented sources are used.

---

## 9. Implementation plan, start to finish

The project was built in gated phases (`docs/master-spec.md`). Each phase began with a plan approved by a human and ended with a report (`docs/phase-reports/`).

| Phase | Goal | What was built | Status |
|---|---|---|---|
| **0. Foundation** | A healthy empty project | Folder structure, config (`src/config/settings.py`), structured logging, pytest, docs skeleton, `AGENTS.md` rules | Done |
| **1. Dataset and ingestion** | Clean, validated data in SQLite | Schema and migrations (`src/db/`), source adapters for Amazon Reviews 2023 and Amazon Berkeley Objects (`src/data/adapters/`), validation, deduplication, statistics, data-source registry | Done |
| **2. Review intelligence** | Aspect sentiment and complaint extraction | Not implemented. The UI says "Aspect analysis not available". | Planned |
| **3. Embeddings and vector stores** | Local product and review indexes | Embedder abstraction, FAISS indexes, a manifest that ties indexes to their database, one-command rebuild | Done |
| **4. Retrieval (RAG v1–v3)** | Semantic, hybrid and reranked retrieval | FTS5 keyword search, FAISS semantic search, metadata filters, Reciprocal Rank Fusion, optional cross-encoder, review-evidence retrieval, token-budgeted evidence packs | Done |
| **5. Local LLM layer** | Reliable structured generation | `LLMProvider` interface, Ollama provider, prompt templates, JSON schema validation with retries, time budget, safe fallback | Done |
| **6. Evidence validation** | Catch unsupported claims | Citation checks, lexical and semantic support scoring, numeric recomputation against the database, support reports | Done |
| **7. Agent and query understanding** | The conversational brain | Intent router, rules-first query parser, session state, clarification, refine and compare flows, out-of-scope handling, FastAPI endpoints | Done |
| **8. Personal memory** | Personalisation | Built, then **removed** on 2026-10-21 by decision: no user profiles, for privacy | Removed |
| **9. Frontend** | The product-card chat experience | Streamlit app with cards, evidence panels, comparison, filter chips, empty and error states, image fallback | Done |
| **10. Evaluation** | Evidence for the report | Labelled query sets, retrieval and strict-matching precision, evidence-validation and latency benchmarks, LLM model and prompt A/B tests | Done |
| **11. Hardening** | Safe, documented, one-command demo | Security review (localhost binding, optional API key), configuration-safety checks, `scripts/run_demo.ps1`, end-to-end and UI check scripts, runbook | Done |

**Work after the core phases:**
1. **Bigger catalogues:**
   - v2: 20,000 Fashion products;
   - v3: 50,000, with strict type, colour and material matching;
   - v4: 86,588, with Indian ethnic wear, jewellery, beauty, home and sports.
2. **Product-first search:** cards appear in seconds without a model call. The AI summary became optional and runs in the background.
3. **INR-only pricing:** documented conversion, labelled estimates, and redaction of foreign amounts.
4. **Validated product images** with fallbacks.
5. **Rebrand** from the working name "ShelfSense" to **Self Search**. Internal module names and environment variables (`SHELFSENSE_*`) are unchanged.

---

## 10. Quality: tests and measured results

**Automated tests:** 1,024 pytest tests, all passing. They cover parsing, filters, fusion, pricing and conversion, evidence validation, API contracts, the frontend view models and the dataset build.

**Live checks** on the running app:
- `scripts/e2e_demo_check.py`: 19 of 19 end-to-end checks pass. They cover search, budgets, ratings, Q&A, comparison, no-match, unknown prices, out-of-scope, sarees, lehengas, earrings and evidence validation.
- `scripts/ui_check.py`: 0 problems across 5 viewports, from 1440 px desktop down to 320 px reflow and 200% zoom.

**Strict-matching precision** (share of returned products that truly meet every requirement; v4 catalogue):

| Query set | Relaxed baseline | Strict (Self Search) |
|---|---:|---:|
| Development set (20 queries) | 62.5% | **100%** |
| Held-out set (12 queries) | 80.0% | **98.3%** |
| New categories: sarees, lehengas, earrings (20 queries) | 80.0% | **96.1%** |

For the new-category set, one judging rule was corrected after the run, and the original result (74.0%) is also reported in `docs/evaluation-results.md`.

**Speed** (CPU only, no GPU):

| Step | Typical time |
|---|---|
| Search | Median 4–5 s on 86,588 products (no model call) |
| AI review summary | About 40–80 s, in the background |
| Question or comparison | About 20–50 s |

---

## 11. Running it

**Prerequisites:**
- Windows 10/11 with PowerShell;
- Python 3.11+ with the project virtual environment;
- [Ollama](https://ollama.com) running, with `ollama pull llama3.2:3b` done;
- internet only for the initial downloads and for showing product photos.

**One-command demo** (current v4 catalogue):
```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_demo.ps1
# then open http://127.0.0.1:8502   (API: http://127.0.0.1:8000/health)

# previous 50,000-product catalogue instead:
powershell -ExecutionPolicy Bypass -File scripts\run_demo.ps1 -Catalogue fashion-v3-20261022T085353Z
```
`run_demo.ps1` checks that the indexes match the database, sets the configuration, and starts the API and UI. Options: `-NoEstimates`, `-NoImages`.

**From scratch:**
```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python -m pytest -q                                   # run the tests
python scripts/migrate_db.py                          # upgrade the schema (backs up first)
python -m uvicorn src.api.app:app                     # API on 127.0.0.1:8000
python -m streamlit run frontend/app.py               # UI
```

**Rebuilding the data** (only if needed; the raw files are about 50 GB):
```powershell
python scripts/fetch_amazon2023.py --group clothing    # download and checksum the source files
python scripts/fetch_amazon2023.py --group lifestyle   # beauty, handmade, sports
python scripts/build_expanded_db.py --run-id <name> --base data/experiments/fashion-v3-20261022T085353Z --selection config/selection_amazon2023_v4_additions.toml
python scripts/validate_product_images.py --run-dir data/experiments/<name>    # validate photo URLs
# build the FAISS indexes and manifest for that database:
$env:SHELFSENSE_DB_PATH = "data/experiments/<name>/shelfsense.db"
$env:SHELFSENSE_VECTOR_STORE_DIR = "data/experiments/<name>/vector_store"
python scripts/generate_embeddings.py --overwrite
```

**Verify a running demo:**
```powershell
python scripts/e2e_demo_check.py --db data\experiments\fashion-v4-20261023T014937Z\shelfsense.db
python scripts/ui_check.py --url http://127.0.0.1:8502 --query "red silk saree"
```

---

## 12. Project structure

```
src/
  agent/         intent router, query parser, constraints, session, controller, presentation, schemas
  api/           FastAPI app (endpoints above)
  retrieval/     keyword (FTS5), semantic (FAISS), filters, strict constraints, RRF fusion, reranker
  llm/           LLMProvider interface, Ollama provider, prompts, JSON validation, time budget
  evidence/      evidence packs, claim validator, numeric verifier, support reports
  pricing/       INR conversion (fx), budgets, synthetic estimates, foreign-price redaction
  embeddings/    offline and Ollama embedders
  vector_store/  FAISS store and index manifest
  data/          dataset adapters, ingestion, selection, deduplication, image lists
  db/            schema, migrations, connection
  config/        settings and logging
frontend/        Streamlit app, API client, view models, card components
scripts/         demo launcher, data fetch/build, image validation, evaluation, e2e and UI checks
config/          exchange rate, estimate ranges, dataset selection rules (TOML)
eval/            labelled query sets and results
tests/           unit and integration tests
docs/            specification, design, data sources, evaluation, limitations, runbook
data/            databases and indexes (not committed)
```

---

## 13. Known limitations

- **Speed:** CPU-only generation takes about 1 minute per AI summary. A GPU would make it interactive.
- **Prices:**
  - 56% of products have no price, including most sarees and lehengas.
  - Converted prices are 2023 US prices at a 2023 rate, not current Indian retail prices.
- **Catalogue gaps:** only 1,378 sarees and 210 lehengas were available (allocations of 8,000 and 7,000). Brand and description coverage is low (8% and 22%).
- **Lexical matching:**
  - "silk" includes art silk;
  - multi-pair earring packs can appear among earrings;
  - keyword-stuffed titles can slip through.
- **Thin evidence:** many new products have only 1–2 reviews, so many claims end up "Not verified". That is the validator working as designed.
- **Licence:** the dataset states no licence, so this is local academic use only.

Full list: `docs/limitations.md`.

---

## 14. Documentation

| Document | Contents |
|---|---|
| `docs/master-spec.md` | Full specification and phase plan |
| `docs/architecture.md` | System architecture |
| `docs/rag-design.md`, `docs/llm-design.md`, `docs/agent-design.md` | Retrieval, LLM and agent design |
| `docs/evidence-validation.md` | How claims are checked |
| `docs/api-documentation.md` | REST API reference |
| `docs/database-schema.md` | Database schema |
| `docs/data-sources.md` | Datasets, licences, build details, prices and images |
| `docs/frontend.md` | UI behaviour and display rules |
| `docs/evaluation-methodology.md`, `docs/evaluation-results.md` | How it was measured and the results |
| `docs/demo-runbook.md` | How to run and present the demo |
| `docs/deployment.md`, `docs/privacy.md` | Deployment, security and privacy |
| `docs/limitations.md` | Known limitations and risks |
| `docs/phase-reports/` | Report for each development phase |

---

## 15. Summary

- **What it is:** a local, private AI shopping assistant that finds products meeting every stated requirement (type, colour, material, occasion, brand, rating, ₹ budget) in an 86,588-product catalogue, and explains them using real customer reviews.
- **Why it matters:**
  - every AI claim cites review IDs and is checked against the evidence and the database;
  - unsupported claims are flagged, and missing data is stated, never invented;
  - this makes it a reference design for trustworthy AI shopping assistants.
- **How it is built:**
  - **API and UI:** FastAPI backend and Streamlit UI;
  - **Retrieval:** hybrid search combining SQLite FTS5 keyword search and FAISS vector search, merged by Reciprocal Rank Fusion, with strict filters;
  - **Generation and validation:** a local Llama 3.2 3B model via Ollama for summaries, Q&A and comparisons, followed by evidence validation;
  - **Cost:** no paid APIs, and it runs offline.
- **Where it stands:**
  - 11 of 12 planned phases are finished; review intelligence is still planned, and personal memory was built and then removed for privacy;
  - 1,024 tests pass, with 96–100% precision on strict-matching evaluations;
  - prices are in rupees only, with documented provenance, and 97% of products have a validated photo;
  - known gaps are speed on CPU, price coverage and thin reviews for new products.

---

*Academic research prototype. Not affiliated with any retailer or brand. Product data and images belong to their owners; the source dataset is used for local academic purposes only.*
