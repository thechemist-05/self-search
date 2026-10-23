# ShelfSense: Evidence-Grounded Conversational Product Discovery Assistant

*Master specification and operating contract for Google Antigravity*

---

## 0. READ THIS FIRST (instructions to the agent)

You are the engineering agent for this project. This document is the single source of truth. Before doing anything else:

1. Read this entire document.
2. Create `AGENTS.md` at the project root from Section 14 of this document.
3. Create `docs/master-spec.md` as a copy of this document.
4. Do **not** write any application code until the human approves Phase 0.

### 0.1 The Phase Gate Protocol (mandatory, never skip)

This project is built **one phase at a time**. The human must approve every phase before you start it.

For every phase, follow this exact cycle:

```
PROPOSE  ->  WAIT FOR APPROVAL  ->  IMPLEMENT  ->  TEST  ->  REPORT  ->  WAIT FOR APPROVAL TO CONTINUE
```

1. **PROPOSE.** At the start of a phase, write a short plan: files you will create or change, libraries you will add, tests you will write, risks. Then **stop and ask:** *"Do you approve starting Phase N?"*
2. **IMPLEMENT** only that phase. Touch nothing outside its scope.
3. **TEST.** Run the tests and checks for that phase. Fix failures before reporting.
4. **REPORT.** Give a phase report: what was built, files created/changed, tests run and results, known issues, decisions you made, and anything you want the human to decide.
5. **STOP.** Ask: *"Phase N is complete. Do you approve moving to Phase N+1?"* Do not begin the next phase until the human says yes.

### 0.2 Hard rules of conduct

- Never implement more than one phase in a single go.
- Never silently change architecture, database schema, dependencies, or API contracts. If a change is needed, propose it and wait for approval.
- Never rewrite working code without being asked.
- If something in this spec is ambiguous or impossible, ask. Do not guess on important decisions.
- If a free/local option does not exist for something, say so. Do not quietly use a paid service.
- Keep commits small, one logical change each, with clear messages.
- Parallel agents are allowed only if the human requests it, and only with strict folder boundaries (one agent per directory).

---

## 1. Project overview

**Name:** ShelfSense **Tagline:** *Tell it what you want. It shows you the products, and proves why.*

ShelfSense is a conversational shopping assistant. A user describes what they want in natural language (for example, *"a lightweight waterproof backpack for daily commute under 3000 rupees"*). The chatbot understands the request, finds matching products from a local product catalogue, **shows them as visual product cards**, and explains why each product fits, using evidence from real customer reviews and product data.

The core idea: the LLM is **not** the database, **not** the search engine, and **not** the source of truth. It is only the language layer. All facts come from retrieved data, and every factual claim shown to the user must be traceable to evidence.

### 1.1 What the user can do

- Search by describing a need in plain language, with optional constraints (price, brand, colour, size, category, rating).
- See matching products as cards (image, title, price, rating, key attributes, why it matches).
- Ask follow-up questions ("does it run small?", "anything cheaper?", "show only black ones").
- Compare two or more products side by side.
- Read an evidence-backed summary of reviews (pros, cons, recurring complaints).
- ~~Save products to a wishlist and keep a personal preference profile that improves later results.~~ **Removed by amendment (2026-10-21):** ShelfSense has no user profiles, wishlist, preferences, feedback or personalised ranking; F10 and the `PERSONAL` intent no longer apply. The profile tables remain until a separate, approved database cleanup.
- See "Why am I seeing this?" for every result, with the actual review snippets and attributes used.

### 1.2 What it is not

- Not a payment or checkout system.
- Not integrated with any real store's private APIs.
- Not allowed to scrape websites that forbid it.
- Not allowed to invent products, prices, ratings, reviews, or specifications.

### 1.3 Context

This is a final-year academic project demonstrating **LLM + RAG + GenAI**. It must be rigorous enough to evaluate experimentally (see Phase 10).

---

## 2. Hard constraints (non-negotiable)

1. **No paid APIs and no API keys that require payment.** No OpenAI, Anthropic, Google Gemini API, Cohere, Pinecone, etc.
2. **LLM runs locally** (via Ollama or equivalent open-weight runtime).
3. **Embeddings run locally** (open-source sentence embedding model).
4. **Vector store runs locally** (FAISS or Chroma).
5. **Relational database runs locally** (SQLite).
6. After datasets, models, and dependencies are downloaded once, the app must **work fully offline**.
7. Every dataset must have a documented source, licence/usage terms, and collection date, and the licence must permit academic use.
8. **Never fabricate data.** Never invent reviews, products, prices, specs, or citations. Sole exception (amendment approved 2026-10-21): clearly labelled synthetic INR price estimates from the versioned, illustrative category-range config (`config/price_estimate_ranges.toml`). They are stored apart from source prices (`price_estimates`, experimental schema v3); they are given only to products without a source price; they are never sent to the LLM, used as evidence or treated as verified; they are never presented as retailer prices; they never come from currency conversion; and they are used for budget filtering only when explicitly enabled (off by default).
9. **Every factual claim in an AI answer must map to retrieved evidence IDs.** If evidence is insufficient, the system must say so plainly.
10. Do not claim affiliation with or integration into any real retailer or brand.
11. No secrets, keys, or private user data committed to Git.

---

## 3. Functional requirements

| ID | Requirement |
| --- | --- |
| F1 | Natural-language product search with constraint extraction (price, brand, category, colour, size, rating) |
| F2 | Hybrid retrieval: semantic + keyword + metadata filtering |
| F3 | Reranking of retrieved candidates |
| F4 | Product cards rendered in the chat with images, price, rating, attributes, and a match explanation |
| F5 | Review intelligence: aspect-based sentiment (e.g. quality, fit, durability, value), complaint extraction |
| F6 | Evidence-grounded answers returned as structured JSON, then rendered for the user |
| F7 | Evidence validation: unsupported claims are flagged or removed |
| F8 | Multi-turn conversation with memory of the current session (filters, shown products) |
| F9 | Product comparison (2 to 4 products) |
| F10 | Personal profile and wishlist stored locally, used to personalise ranking and answers |
| F11 | "Why this result?" panel showing evidence |
| F12 | Graceful handling of vague, out-of-scope, or unsupported queries |
| F13 | Evaluation harness comparing system variants |

## 4. Non-functional requirements

- **Local-first and private:** no user data leaves the machine.
- **Reproducible:** pinned dependencies, deterministic ingestion, seeded randomness.
- **Modular:** each layer replaceable (LLM provider, embedding model, vector store).
- **Testable:** unit tests for every module, integration tests per phase.
- **Performance target:** a typical query returns product cards within about 3 seconds and a full answer within about 15 seconds on a normal laptop (state your hardware assumptions in the docs).
- **Observable:** structured logging of query, retrieved IDs, scores, and latency per stage.
- **Documented:** every phase updates the relevant file in `docs/`.

---

## 5. System architecture

```
                          USER
                            |
                            v
                  +-------------------+
                  |     Frontend      |
                  | (Streamlit, v1)   |
                  +---------+---------+
                            |
                            v
                  +-------------------+
                  |   Backend API     |
                  |   (FastAPI)       |
                  +---------+---------+
                            |
                            v
                  +-------------------+
                  |  Agent Controller |
                  |  Intent Router    |
                  |  Query Parser     |
                  +---------+---------+
                            |
        +-------------------+-------------------+
        v                   v                   v
  Product Search       Review Insight      Personal Memory
  (hybrid retrieval)   (aspects, RAG)      (profile, wishlist)
        |                   |                   |
        +-------------------+-------------------+
                            v
                  Evidence Assembly
                            |
                            v
                  Local LLM (Ollama)
                            |
                            v
                  Evidence Validator
                            |
                            v
               Product cards + answer + evidence
```

### 5.1 Core data flow for a search query

1. User message arrives.
2. **Query parser** extracts intent, product need, and constraints into structured JSON (the LLM may help, but results are validated against a schema).
3. **Hybrid retrieval** returns candidate products: semantic (embeddings) + keyword (BM25 via SQLite FTS5) + metadata filters.
4. **Reranker** reorders candidates.
5. **Review retrieval** pulls the most relevant review chunks for each top product.
6. **Evidence assembly** builds a compact, ID-labelled evidence pack.
7. **LLM** produces structured JSON referencing evidence IDs only.
8. **Evidence validator** checks every claim against the pack. Unsupported claims are dropped or flagged.
9. **UI** renders product cards plus explanation plus "Why this result?".

---

## 6. Technology stack (all free and open source)

| Layer | Choice | Notes |
| --- | --- | --- |
| Language | Python 3.11+ |  |
| Backend API | FastAPI + Uvicorn |  |
| Frontend (v1) | Streamlit | Custom components allowed. React frontend only if the human approves later |
| Relational DB | SQLite (with FTS5) | Products, reviews, users, wishlist, logs |
| Vector store | FAISS (default) | Chroma acceptable if justified in the plan |
| Embeddings | `BAAI/bge-small-en-v1.5` or `sentence-transformers/all-MiniLM-L6-v2` | Provider abstraction so it can be swapped |
| Reranker | Cross-encoder such as `cross-encoder/ms-marco-MiniLM-L-6-v2` | Optional in early phases |
| LLM runtime | Ollama | Small open-weight model (for example a 3B to 8B instruct model). Choose based on hardware, state the choice in the plan |
| Sentiment / aspects | Classical NLP plus open-source models (spaCy, transformers) | Start simple |
| Testing | pytest, pytest-cov |  |
| Quality | ruff, black, mypy (optional) |  |
| Config | `pydantic-settings` plus `.env` |  |

If you want to use anything not listed, propose it and wait for approval.

---

## 7. Dataset strategy

The app needs: **products** (title, description, category, brand, price, images, attributes) and **reviews** (text, rating, date, product link).

**Rules**

- Use only openly available datasets whose licence allows academic use. Candidates to evaluate (verify the current licence and availability yourself, do not assume):
  - Amazon Reviews 2023 (McAuley Lab) for products plus reviews, a small number of categories only.
  - Other open product and review datasets on Hugging Face Datasets or Kaggle with clear licences.
- Start with **one or two product categories** and a manageable subset (for example 5,000 to 20,000 products and a few hundred thousand reviews at most). The design must scale, but v1 must run on a laptop.
- Every dataset gets an entry in `docs/data-sources.md` and a row in the `data_sources` table: name, URL, licence, date downloaded, preprocessing steps, row counts.
- Do not scrape any commercial website.
- If product images are not available offline, use image URLs when online and a placeholder when offline. Never generate fake product photos.
- Raw data goes in `data/raw/` (git-ignored). Processed data goes in `data/processed/`.

---

## 8. Database schema (starting point, refine in Phase 1 plan)

```
data_sources(id, name, url, license, downloaded_at, notes)
categories(id, name, parent_id)
brands(id, name)
products(id, source_id, external_id, title, description, category_id, brand_id,
         price, currency, avg_rating, rating_count, image_url, attributes_json)
reviews(id, product_id, source_id, external_id, rating, title, text, review_date,
        helpful_votes, verified_flag)
review_analysis(review_id, aspects_json, sentiment, complaints_json, topics_json, confidence)
product_aspect_summary(product_id, aspect, positive_count, negative_count, neutral_count)
users(id, display_name, created_at)
user_preferences(user_id, key, value)
wishlist(user_id, product_id, added_at)
conversations(id, user_id, started_at)
messages(id, conversation_id, role, content, structured_json, created_at)
query_logs(id, query, parsed_json, retrieved_ids_json, latency_json, created_at)
```

Schema changes after Phase 1 need explicit approval and a migration.

---

## 9. RAG design

**Version 1: Semantic RAG.** Query -> embedding -> FAISS -> top-K. **Version 2: Hybrid RAG.** Semantic + BM25 (FTS5) + metadata filters, merged with Reciprocal Rank Fusion. **Version 3: Reranked hybrid.** Add a cross-encoder reranker. **Version 4: Personalised RAG.** Add user profile and wishlist signals to ranking and answers.

Two indexes:

- **Product index:** one vector per product (title + description + key attributes).
- **Review index:** review chunks (cleaned, chunked, each tagged with `review_id` and `product_id`).

Critical rule: **never send all reviews to the LLM.** Retrieve only relevant, deduplicated, ID-labelled evidence within a fixed token budget.

Keep each version selectable by a config flag so Phase 10 can compare them.

---

## 10. LLM layer design

- Provider abstraction (`LLMProvider` interface) with an Ollama implementation. No paid providers.
- Structured prompting. The LLM receives: user question, parsed constraints, product records, evidence pack (with IDs), optional user profile.
- The LLM **must return JSON** that matches a schema, for example:

```json
{
  "summary": "...",
  "recommendations": [
    {
      "product_id": "P123",
      "why_it_matches": [
        {"claim": "...", "evidence_ids": ["R88", "R412"]}
      ],
      "watch_outs": [
        {"claim": "...", "evidence_ids": ["R17"]}
      ]
    }
  ],
  "insufficient_evidence": false
}
```

- Validate output against the schema. On invalid JSON, retry a limited number of times, then fall back to a safe template answer.
- The LLM must never invent products, prices, specs, ratings, reviews, or user history.
- Numeric claims (such as "17% of negative reviews mention stitching") must be **computed by code** from the database, not by the LLM.

---

## 11. Agent design

An intent router decides how to handle each message:

| Intent | Example | Handling |
| --- | --- | --- |
| `PRODUCT_SEARCH` | "waterproof backpack under 3000" | Hybrid retrieval -> cards |
| `REFINE_SEARCH` | "only black ones", "cheaper" | Update constraints on session state, re-run |
| `PRODUCT_QUESTION` | "does it run small?" | Review RAG on the selected product |
| `COMPARE` | "compare these two" | Side-by-side attributes + review aspects |
| `PERSONAL` | "something like what I saved" | Personal RAG over wishlist and profile |
| `SMALLTALK` / `OUT_OF_SCOPE` | greetings, unrelated | Brief reply, steer back |
| `CLARIFY` | "I want something nice" | Ask one focused clarifying question |

Start with a rule-based plus lightweight classifier router. Do not over-engineer into an autonomous multi-agent system.

---

## 12. UI / GenAI experience

This should **not** look like a plain text chatbot. Layout:

- **Left/main:** chat conversation.
- **Inline in chat:** horizontally scrollable **product cards** (image, title, price, rating, 2 to 3 key attribute chips, "Why this?" button, "Save" button, "Compare" checkbox).
- **Detail panel (on card click):** full product info, aspect sentiment bars (quality, value, durability, etc.), review highlights, complaints, and the **Evidence** list with real review snippets and IDs.
- **Sidebar:** active filters (editable chips), wishlist, preference profile, settings (retrieval mode, model).
- **Compare view:** table of 2 to 4 products with aspects and price.
- Always show an "Insufficient evidence" notice instead of guessing.
- Show a footer disclaimer: *This is an academic research prototype and is not affiliated with any retailer.*

The human will give precise visual feedback iteratively. Implement changes directly as requested, in small steps.

---

## 13. Evaluation methodology

Build evaluation early, not in the last week.

**Retrieval:** Recall@K, Precision@K, MRR, NDCG@K, using a hand-built labelled query set (target 50+ queries, store in `eval/queries.jsonl`). **Generation:** faithfulness (claims supported by evidence), answer relevance, context relevance, citation accuracy, hallucination rate. **System:** latency per stage, memory use, error rate.

**Compare four variants on the same query set:**

```
A: LLM only (no retrieval)
B: LLM + semantic RAG
C: LLM + hybrid RAG + reranker
D: LLM + hybrid RAG + reranker + personal RAG
```

Produce tables and charts into `docs/evaluation-results.md`. Report honest results, including where a layer did not help.

---

## 14. Contents for `AGENTS.md` (create this at project root)

```
# ShelfSense

## Purpose
Academic project: a local, evidence-grounded conversational product discovery
assistant using LLM + RAG + GenAI. Not affiliated with any retailer.

## Operating protocol
- Work in phases defined in docs/master-spec.md.
- At the start of every phase: propose a plan and WAIT for approval.
- At the end of every phase: report and WAIT for approval to continue.
- Never implement more than one phase at a time.
- Never change architecture, schema, dependencies or API contracts without approval.

## Constraints
- No paid APIs or paid keys. Local LLM (Ollama), local embeddings, FAISS/Chroma, SQLite.
- Must run offline after initial downloads.
- Never fabricate products, prices, reviews, ratings, specs, or citations.
  Sole exception (amendment approved 2026-10-21): clearly labelled synthetic INR price estimates from the versioned, illustrative category-range config (`config/price_estimate_ranges.toml`). They are stored apart from source prices (`price_estimates`, experimental schema v3); they are given only to products without a source price; they are never sent to the LLM, used as evidence or treated as verified; they are never presented as retailer prices; they never come from currency conversion; and they are used for budget filtering only when explicitly enabled (off by default).
- Every factual AI claim must reference retrieved evidence IDs.
- If evidence is insufficient, say so explicitly.
- No scraping of commercial sites. Datasets need documented licence and source.

## Workflow per task
Explore -> Plan -> Implement smallest version -> Write tests -> Run tests ->
Fix -> Document -> Small commit.

## Code rules
- Python 3.11+, type hints, docstrings on public functions.
- Modular layers behind interfaces (LLMProvider, Embedder, VectorStore).
- Tests for every module (pytest). Do not leave failing tests.
- Do not rewrite working components unnecessarily.

## Git rules
- Small commits. Never commit .env, secrets, large model files, raw datasets,
  or private user data.
```

---

## 15. Project folder structure

```
shelfsense/
  .agents/
    rules/
    skills/
  AGENTS.md
  README.md
  requirements.txt
  pyproject.toml
  .env.example
  .gitignore
  data/
    raw/            (git-ignored)
    processed/
  models/           (git-ignored)
  eval/
    queries.jsonl
    results/
  docs/
    master-spec.md
    architecture.md
    data-sources.md
    database-schema.md
    rag-design.md
    llm-design.md
    agent-design.md
    evaluation-methodology.md
    evaluation-results.md
    privacy.md
    limitations.md
    api-documentation.md
    deployment.md
    phase-reports/
  scripts/
  src/
    config/
    data/             (ingestion, validation)
    db/               (models, migrations)
    reviews/          (preprocessing, aspects, sentiment)
    embeddings/
    vector_store/
    retrieval/        (semantic, keyword, hybrid, rerank)
    llm/              (provider abstraction, prompts, schemas)
    evidence/         (assembly, validation)
    agent/            (router, query parser, session state)
    personal/         (profile, wishlist)
    api/              (FastAPI)
    evaluation/
  frontend/           (Streamlit app and components)
  tests/
    unit/
    integration/
```

---

## 16. Development phases

Each phase ends with a **STOP and ask for approval** per Section 0.1. Every phase must list its tests and an exit checklist in its report.

### Phase 0: Foundation

**Goal:** an empty but healthy project. **Tasks:** create folder structure, `AGENTS.md`, `docs/master-spec.md`, config system, logging setup, `.env.example`, `.gitignore`, `requirements.txt`, pytest setup, README skeleton, docs skeleton. **Do not** implement data, RAG, LLM, or UI. **Tests:** project imports, config loads, logging works, pytest runs. **Exit:** `pytest` passes, structure matches Section 15.

### Phase 1: Dataset and ingestion

**Goal:** clean, validated product and review data in SQLite. **Tasks:** evaluate and propose candidate datasets with licences (wait for the human to choose); build data source registry; create the schema and migrations; write ingestion scripts; normalise fields; deduplicate; missing-value analysis; data statistics report; preserve source metadata. **Tests:** ingestion on a tiny fixture, validation rules, duplicate detection, idempotent re-runs. **Exit:** database populated, `docs/data-sources.md` and `docs/database-schema.md` written, statistics report generated.

### Phase 2: Review intelligence

**Goal:** structured insight per review and per product. **Tasks:** text cleaning, near-duplicate review detection, aspect extraction, aspect-based sentiment, complaint extraction, topic clustering, per-product aspect summaries (counts computed by code). Start with classical NLP and small open models. **Tests:** known-sentence fixtures for aspects and sentiment, summary counts match raw data. **Exit:** `review_analysis` and `product_aspect_summary` populated, accuracy spot-check on 100 manually checked reviews reported.

### Phase 3: Embeddings and vector stores

**Goal:** local product and review indexes. **Tasks:** embedder abstraction, product index, review chunking and review index, persistence and reload, index build script. **Tests:** embedding shape and determinism, index round-trip, nearest-neighbour sanity checks. **Exit:** indexes build from the database in one command and reload correctly.

### Phase 4: Retrieval (RAG versions 1 to 3)

**Goal:** semantic, hybrid, and reranked retrieval, switchable by config. **Tasks:** semantic retrieval; FTS5 keyword retrieval; metadata filters; Reciprocal Rank Fusion; optional cross-encoder reranker; review evidence retrieval per product; token-budgeted evidence packs. **Tests:** retrieval on a small labelled set, filter correctness, fusion unit tests. **Exit:** a CLI script can run any retrieval mode and print ranked results with scores.

### Phase 5: Local LLM layer

**Goal:** reliable structured generation from a local model. **Tasks:** `LLMProvider` interface, Ollama implementation, prompt templates, JSON schema validation with limited retries, safe fallback, insufficient-evidence handling, latency logging. **Tests:** schema validation, malformed-output recovery (mock the LLM), integration test with the real local model. **Exit:** given a question and an evidence pack, the system returns valid, ID-referenced JSON.

### Phase 6: Evidence validation

**Goal:** catch unsupported claims. **Tasks:** claim-to-evidence checker (evidence IDs exist, snippets actually support the claim using lexical and embedding similarity), numeric claim recomputation from the database, flag or remove unsupported claims, produce a support report per answer. **Tests:** crafted supported and unsupported claims, fake evidence IDs rejected. **Exit:** measurable unsupported-claim detection on a small test set.

### Phase 7: Agent and query understanding

**Goal:** the brain of the chatbot. **Tasks:** query parser (constraints to structured filters), intent router, session state (active filters, shown products), clarification logic, refine and compare flows, out-of-scope handling, FastAPI endpoints. **Tests:** intent and constraint extraction on a labelled query list, multi-turn refinement scenarios, API tests. **Exit:** end-to-end conversation works via API (no UI yet).

### Phase 8: Personal memory

**Goal:** local personalisation. **Tasks:** users, preferences, wishlist, feedback tables and services; personalised re-ranking; personal RAG ("like what I saved"); privacy controls (export and delete my data). **Tests:** personalisation changes ranking in expected ways, data deletion works. **Exit:** personal signals visibly influence results and explanations, all data stays local.

### Phase 9: Frontend

**Goal:** the product-card chat experience from Section 12. **Tasks:** Streamlit app using the existing API contract, chat, product cards, detail panel with evidence, compare view, sidebar filters and wishlist, empty and error states, offline-safe image placeholders. **Do not** change backend contracts. If a change is needed, propose it. **Tests:** UI smoke tests, API contract tests, manual checklist. **Exit:** a full demo flow works: search, refine, open evidence, compare, save.

### Phase 10: Evaluation and comparison

**Goal:** experimental evidence for the report. **Tasks:** build the labelled query set, metrics code, run variants A to D, generate tables and charts, error analysis, latency and memory measurements. **Tests:** metric functions verified on toy examples. **Exit:** `docs/evaluation-results.md` complete and honest.

### Phase 11: Hardening and documentation

**Tasks:** security and privacy review, dependency audit, error handling pass, performance tuning, finish all docs in Section 15, one-command setup and run scripts, final demo script. **Exit:** a fresh machine can set up and run following the README alone.

---

## 17. Testing strategy

- **Unit tests** for every module, **integration tests** at the end of every phase.
- Mock the LLM in most tests, run a small number of real-model tests marked `@pytest.mark.slow`.
- Fixtures: a tiny hand-made dataset (about 20 products, 100 reviews) committed to the repo.
- Run the full suite before every phase report. Report the exact commands and results.
- If a test fails: diagnose the root cause, fix only that issue, re-run the affected tests, then the full suite.

---

## 18. Security and privacy

- All processing local. No telemetry, no external calls at runtime (except optional image URL loading, which must be disableable).
- Sanitise all user input. Prevent prompt injection from review text: treat reviews strictly as quoted data, never as instructions.
- No secrets in code. Provide `.env.example` only.
- User data export and delete functions.
- Document limitations and risks in `docs/limitations.md` (dataset bias, small local model errors, review spam, sentiment errors).

---

## 19. Final deliverables

1. Working application (backend, frontend, local models).
2. Complete repository with tests passing.
3. All documentation in `docs/`.
4. Evaluation report with variant comparison.
5. Demo script and sample queries.
6. One-command setup instructions.

---

## 20. Your first actions (agent checklist)

1. Confirm you have read this document.
2. Create `AGENTS.md` (Section 14) and `docs/master-spec.md`.
3. Write a **Phase 0 proposal** (files, dependencies, tests, risks).
4. Ask: **"Do you approve starting Phase 0?"**
5. Stop and wait. Do not write any other code.

---

## Appendix A: How the human should work with you (for reference)

- Approve with a clear "approved, start Phase N".
- To request changes: "change X in Phase N, do not touch anything else".
- If a phase misbehaves: "stop, explain what you changed, and revert to the last commit".
- For UI work: the human will describe visual changes precisely. Implement them directly without presenting multiple options, in small steps.
