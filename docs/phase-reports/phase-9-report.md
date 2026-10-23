# Phase 9 Report: Frontend

**Date:** 2026-10-19
**Status:** Complete. The concurrency defect found during this phase (Section 7) is fixed under the approved Option A and verified. **Awaiting your own visual inspection of the website before Phase 10.** I have not visually tested it in a browser.

---

## 1. Approved Decisions and How They Were Applied

| Decision / requirement | Implementation |
|---|---|
| Additive `/chat` fields `result_product_ids`, `products`, `evidence`, `preference_suggestions` | All default to `[]`; every pre-existing field and its meaning is unchanged. Tested by `TestCompatibility` and the Phase 8 key-set test (updated to the new, exact key set) |
| `generation` field from the actual outcome | `LLMService.generate_answer_with_mode()` records `llm`/`fallback`/`none` where it happens, including a fallback after a model error mid-request while `is_available()` was true. `generate_answer()` is unchanged and delegates to it |
| Optional `product_ids` request field | Validated before any state change (1–4, no duplicates, existing session, all shown in it) → `422` otherwise. Tested with 6 invalid cases, each checked for no session-history change |
| `GET /products/{id}`, `DELETE /sessions/{id}/filters/{name}` | Implemented. Read-only from the DB, and session-only respectively |
| Wrapping Streamlit card grid | `st.columns(3)`, which stacks on narrow screens |
| "Aspect analysis not available" | Shown on cards, in details and in the compare table. `aspects` passes through only stored `product_aspect_summary` rows (none exist). Nothing is synthesised |
| Cards from DB only | `ProductPresenter` reads by ID. LLM titles, prices and invented product IDs are ignored; tested with an LLM answer containing a fake title, a fake price and product 999 |
| Evidence = the validated pack | `HandlerResult.evidence_pack` is the object passed to `EvidenceValidator.validate_answer`. Tested with a spy around the **real** validator on search and question paths |
| Phase 6 validator in every answer path | Unchanged. No thresholds modified. Search, refine, question, compare and `PERSONAL` all validate; a model-error fallback is still validated (tested) |
| Summary labelled; persistent invalid-report warning; insufficient-evidence message | "AI-written summary · not individually verified". Red banner whenever `is_valid` is false or the report is missing. Notice with `missing_information` |
| Non-persistent suggestions; Save merges; Dismiss has no side effects | The server only returns suggestions. UI **Save** = `GET` then `PUT` with a merged list (other keys untouched); **Dismiss** makes no call. Both are tested by recording every client call |
| Placeholders for fake URLs; remote images only when explicitly true | Neutral SVG placeholder with alt text for missing URLs, remote images off (the default), reserved domains (`example.com` etc.) and failed fetches. Fetching happens server-side with a type check, a 2 MB cap and a 5 s timeout |
| Consent, wishlist, feedback, export and delete preserved; loopback ports; key never in the browser | Each UI control maps to one existing endpoint. Delete needs `DELETE` typed. The key is used only in the Streamlit server process; a test asserts it never appears on the page |
| Streamlit installed as declared | `pip install streamlit` → 1.66.0. `requirements.txt` unchanged |

## 2. Features

**Backend** (`src/agent/presentation.py`, `src/agent/preference_suggestions.py`, `schemas.py`, `controller.py`, `intent_router.py`, `session.py`, `src/llm/service.py`, `src/llm/schemas.py`, `src/api/app.py`):
- Product cards, the evidence list and generation info on every answer turn.
- Validated explicit product selection.
- Deterministic preference-statement detection, checked against the catalogue and stored preferences.
- `GET /products/{id}` and `DELETE /sessions/{id}/filters/{name}`.

**Frontend** (`frontend/`): a full rewrite of the 58-line `/ask` stub. Documented in [docs/frontend.md](../frontend.md).
- **Chat:** search, refine, follow-up questions, "Ask about this product", and comparing 2–4 ticked products with a side-by-side table.
- **Cards** from DB fields: catalogue rating labelled separately from local review count, a "Recommended" or "Also matched" label, and features.
- **Claim display:** every claim carries an icon plus a worded validation status. "Why this result?" shows cited snippets, missing citations, uncited evidence, which filters the product meets, and personal-ranking reasons.
- **Sidebar:** connection status, filter chips (✕ removes one), profile create/open, consent toggle, preferences, wishlist, JSON export download, and guarded delete.
- **States:** spinner, API unreachable (input disabled), local model offline, fallback answer, error messages for 422/404/409/503/5xx, empty results, image placeholders.
- **Text safety:** untrusted review and AI text is markdown-escaped.
- **Config:** `.streamlit/config.toml` binds to loopback and disables Streamlit telemetry. In compose, the UI reaches the API at `http://api:8000`, the key stays server-side, and the host port stays loopback-only.

## 3. Files

**Created:**
- `src/agent/presentation.py`, `src/agent/preference_suggestions.py`
- `frontend/config.py`, `frontend/api_client.py`, `frontend/view_models.py`, `frontend/state.py`
- `frontend/components/__init__.py`, `frontend/components/sidebar.py`, `frontend/components/turn.py`
- `.streamlit/config.toml`
- `tests/unit/test_presentation.py`, `tests/unit/test_frontend_view_models.py`
- `tests/integration/test_chat_contract.py`, `tests/integration/test_frontend_app.py`
- `docs/frontend.md`, `docs/phase-reports/phase-9-report.md`

**Modified:**
- Code:
  - `src/agent/schemas.py`: `ProductCard`, `EvidenceSnippet`, `PreferenceSuggestion`, new optional response fields
  - `src/agent/controller.py`: `product_ids` validation, presentation fields, `InvalidProductSelectionError`
  - `src/agent/intent_router.py`: generation mode, evidence pack on results, explicit selection
  - `src/agent/session.py`: `clear_filter`
  - `src/llm/schemas.py`: `GenerationInfo`
  - `src/llm/service.py`: `generate_answer_with_mode`
  - `src/api/app.py`: `product_ids`, two endpoints, 422 mapping
  - `frontend/app.py`: rewritten
  - `docker-compose.yml`: UI address, API URL, key
- Tests:
  - `tests/conftest.py`: `wire_generation_mode` helper
  - The mock fixtures of 5 existing test files gained one line each so `MagicMock(spec=LLMService)` answers the new method; no assertions changed
  - `tests/integration/test_personal_api.py`: exact key set
  - `tests/unit/test_deployment_config.py`: +2 tests
- Docs: `api-documentation.md`, `agent-design.md`, `llm-design.md`, `limitations.md`, `deployment.md`, `README.md`

The README previously claimed "aspect-based sentiment, complaint extraction" as a feature. It now says this is planned but not implemented.

**Before Phase 9 planning** (approved deployment-security work): loopback port bindings in `docker-compose.yml`, the `Dockerfile` comment, constant-time API-key comparison, `docs/deployment.md`, and `tests/unit/test_deployment_config.py`.

**Concurrency fix** (Section 7, approved Option A): `src/db/connection.py` gains the additive `SerializedConnection` and `SerializedCursor`; `src/api/app.py` uses them; `src/personal/repository.py` shares the connection's lock. New tests: `tests/unit/test_serialized_connection.py` and `tests/integration/test_concurrency.py`. Docs: `limitations.md`.

**Unchanged:** database schema (v2), `EvidenceValidator`, retrieval, and personal-memory semantics. The migration backup `data/processed/shelfsense.v1.20261019T092541Z.bak.db` is preserved.

## 4. Tests

```
.\venv\Scripts\python.exe -m pytest tests/unit tests/integration -v
```

**Before the concurrency fix:** 479 passed, 1 skipped, 0 failed was the usual result. Of 19 full runs, **16 were clean and 3 reported `478 passed, 1 failed, 1 skipped`**. The captured failure was `tests/integration/test_agent_api.py::test_api_connection_usable_from_worker_threads` (`fetchone()` returned `None`). The other two were not captured individually but had the same totals. This was a real defect, not a test bug; see Section 7.

**After the fix** (Section 7): **10 consecutive full runs, each exactly `490 passed, 0 failed, 1 skipped`**, plus 25 consecutive runs of the three concurrency tests, each `12 passed, 0 failed`. The skip is always `tests/integration/test_llm_ollama.py` (Ollama is not installed); that's an environment condition.

New in Phase 9: **119 tests** (360 → 479):

| File | Tests | Covers |
|---|---|---|
| `tests/unit/test_presentation.py` | 35 | Result ordering (the LLM can't add products); cards from DB incl. malformed attributes, unknown IDs and stored-only aspects; product detail; evidence mirrors the pack; generation `llm` / retries / `fallback` ×4 reasons / `none`; preference detection (7 positive, 5 negative cases); catalogue checks, conflicts, already-stored values; suggester writes nothing; merging |
| `tests/integration/test_chat_contract.py` | 19 | Exact response keys and old-field meanings; safe defaults; DB cards vs. a fabricated LLM title, price and product; evidence equals the validated pack (search and question); generation `llm` and model-error fallback, still validated; compare via `product_ids`; 6 invalid selections → 422 with no state change; smalltalk not overridden; suggestion → nothing stored until an explicit `PUT`; product detail; filter clearing |
| `tests/unit/test_frontend_view_models.py` | 33 | Claim status mapping (unmatched → "Unverified"); text labels; banners; 7 image-plan cases; alt and HTML escaping; personalisation lines; filter checks and chips; compare rows; markdown escaping; merging; suggestion notes; API client headers, body, 7 error kinds, unreachable, image-fetch rules; config defaults (remote images off) |
| `tests/integration/test_frontend_app.py` | 30 | The real app under `AppTest`: DB values shown and the LLM title not; statuses, summary label, validation banner; evidence and missing citation; insufficient, empty and fallback states; compare table; offline-model and unreachable states; error messages; 409 handling; placeholders, reserved domains, remote success and failure; suggestion render / Dismiss / Save-merge; consent; wishlist and feedback; no personal buttons without a profile; create profile; export; delete disabled until `DELETE`; compare and ask-about send `product_ids`; filter chip; labels and alt text; **API key never rendered**; disclaimer; `$` escaped |
| `tests/unit/test_deployment_config.py` | +2 | UI env and container address in compose; Streamlit loopback and telemetry off |

All Phase 9 tests are offline and deterministic. No Ollama, no network.

## 5. Live Run (uvicorn + Streamlit, copy of the v2 database, Ollama offline)

- **API** on `127.0.0.1:8770`, **UI** on `127.0.0.1:8771` (headless). Both listened on `127.0.0.1` only: they connected locally and **refused** on the LAN address `192.168.1.33`. UI `/_stcore/health` → `200 ok`; `/` → 200.
- **The real UI script, with the real HTTP client, was driven against the live API** through `AppTest`. Every step passed:

| Step | Result |
|---|---|
| Page load | Offline-model banner shown |
| Create profile → consent toggle | Consent false by default → true via API |
| "I prefer ClickMaster, show me wireless gadgets under $80" | 6.8 s. `generation: fallback/provider_unavailable`; `support_rate 0.22`, `is_valid false` → **validation banner and fallback warning shown**; summary labelled; DB titles on cards; personalisation applied |
| Suggestion | `preferred_brands: ClickMaster` shown; **stored preferences `[]` before Save**; after Save `["ClickMaster"]` |
| "only 4+ stars" | `REFINE_SEARCH`; chips "Max price: 80.0" and "Min rating: 4.0" |
| Ask about card → "is it durable?" | `PRODUCT_QUESTION`, `result_product_ids [10]`, evidence only for product 10 |
| "is the second one comfortable?" | Positional follow-up resolved |
| Tick 2 → Compare | 8.2 s, `COMPARE [2, 10]`, table rendered, support report present |
| Save to wishlist / 👎 | API shows wishlist `[2]` and feedback `(2, down)` |
| Load stored reviews | 10 local reviews listed; "Aspect analysis not available" |
| "something like what I saved" | `PERSONAL`, `seed_records [wishlist#1]`, validated |
| Filter chip ✕ Max price | API session `max_price None`, `min_rating 4.0` kept |
| Export → delete | Export prepared (wishlist 1). Delete stayed disabled until `DELETE` was typed; then the API returned 404 for the user and the UI profile was cleared |

API log: 0 exceptions, 0 × 5xx. UI log: 0 errors.

**Two of my scripted steps first failed because my script was wrong:**
- It tried to compare with only one product ticked; the app correctly kept the button disabled.
- It expected a filter chip after a `PERSONAL` turn, which correctly resets filters.

I re-ran both with corrected scripts, and both passed.

## 6. Not Tested

- **A real browser.** The UI was driven headlessly. Not verified: visual layout, the 400 px narrow view, keyboard-only navigation, screen readers, colour contrast, cross-browser behaviour. A manual checklist is in `docs/frontend.md`.
- **Docker / compose:** Docker isn't installed here; checked statically only.
- **A live LLM:** Ollama isn't running, so every live answer used the fallback path. `mode: "llm"` is covered only by tests with a scripted model.
- **Real remote images:** all catalogue URLs are `example.com` placeholders. Remote loading was tested only with a fake client.
- **Concurrent requests** were unsafe before the fix; see Section 7. They are now covered by an overlapping-HTTP regression test.

## 7. Concurrency Defect: Found, Then Fixed (approved Option A)

### 7.1 Finding (as originally reported)

**Finding.** The flaky test above is correct. In Phase 7 I introduced one shared `sqlite3` connection with `check_same_thread=False` and documented it as safe for concurrent reads. **That is false.** Reproduced in isolation with 8 threads and 4,000 short queries on one connection: **1,833 returned wrong or empty results** (`sqlite3.threadsafety == 3`). With every query serialised by one lock: **0 failures**.

**Impact.** Only when two API requests overlap: two browser tabs, parallel scripts, or a request arriving during another. One UI tab makes its calls one after another, which is why every demo and live run was clean. When requests do overlap, retrieval, card, validation or personal-memory reads can return wrong rows or nothing. That is a correctness risk for the "never show wrong product facts" rule, not just a crash risk.

I proposed two fixes: (A) one lock serialising database access, or (B) per-request connections. **You approved A.**

### 7.2 Fix

| File | Change |
|---|---|
| `src/db/connection.py` | **New, additive** `SerializedConnection` (a `sqlite3.Connection` subclass with one `threading.RLock`) and `SerializedCursor`. Every `execute`/`executemany`/`executescript` runs **and fetches all result rows** while holding the lock; `fetchone`/`fetchmany`/`fetchall`/iteration then read from that buffer and never touch the connection. `commit`/`rollback` take the lock. `get_connection()` is unchanged |
| `src/api/app.py` | `open_api_connection()` passes `factory=SerializedConnection` (one argument); docstring updated |
| `src/personal/repository.py` | Uses the connection's lock when it has one, so a SAVEPOINT transaction excludes **every** other user of the connection for its whole duration, not just other repository calls |

**How it differs from the proposal's wording.** A locks *each database operation*, not each whole HTTP request, which is the smallest change that serialises access to the connection. That meets your "lock covers the complete operation, including fetching" requirement. It also means LLM generation (up to ~15 s with a real model) runs without holding the lock and does not block other requests. API contracts, response fields, `EvidenceValidator` and all security controls are untouched.

**Two pitfalls caught while prototyping:**
1. Locking only `execute()` is not enough: another thread can run between this thread's `execute()` and its `fetchone()`. Hence the full fetch under the lock.
2. CPython's C-level `Connection.execute()` does **not** go through an overridden `cursor()`. My first prototype silently returned plain cursors and still failed, so the `execute*` shortcuts are overridden explicitly (tested).

**Deadlocks.** There is exactly one lock, and it is re-entrant. Code holding it (a repository transaction) can run its own statements, and no code holds it while waiting on another lock. Tested: re-entrant use completes within a 5 s timeout.

### 7.3 Regression tests (no test weakened, skipped or deleted)

| Test | What it proves |
|---|---|
| `test_agent_api.py::test_api_connection_usable_from_worker_threads` | The original failing test, **unchanged** |
| `tests/unit/test_serialized_connection.py` (10) | Cursor compatibility (all entry points, fetch methods, iteration, `Row`, `lastrowid`/`rowcount`, transactions, `executescript`); results buffered; `get_connection` unchanged; **3 × 3,000** overlapping 8-thread queries with zero wrong results; an open repository transaction blocks other threads, which never see half-finished state; re-entrant use does not deadlock |
| `tests/integration/test_concurrency.py` (1) | **Real uvicorn server** on 127.0.0.1. Real FTS retrieval, evidence packs, `EvidenceValidator`, `LLMService` (scripted offline model), cards and personal memory, all on one shared connection. **84 concurrent `/chat` requests** (12 workers), while other threads continuously hit `/health`, `/products/3` and `/users/{id}/export` and churn wishlist writes. Every response must equal a sequential baseline: intent, product IDs, DB card values, evidence IDs and texts, claim statuses, generation mode, personalisation |

**The tests detect the defect.** With the fix reverted in-process (plain `sqlite3.Connection`), the overlapping-HTTP test failed with `NoneType` errors, and the server logged `presentation_cards_failed … expected 5, got 0`, meaning product-card queries were corrupted. The transaction test and the original Phase 7 test also failed.

### 7.4 Results

- **10 consecutive full suites:** run 1–10 each exactly **490 passed, 0 failed, 1 skipped** (73–87 s per run).
- **25 consecutive runs of the three concurrency tests:** each **12 passed, 0 failed**.
- Before the fix, 3 of 19 full runs failed. No intermittent failure has been observed since the fix.

This is strong evidence, but repeated passing runs cannot prove that no race remains. Remaining limits are listed in `limitations.md`:
- multi-query reads such as export are not a single snapshot;
- in-memory session state is not locked against two simultaneous requests on the *same* session;
- external scripts rely on SQLite's file locking.

## 8. Other Limitations (documented in `limitations.md`)

- **Offline answer quality:** answers still score low support rates (0.22 in this run) and are labelled as fallback; validation was not relaxed.
- **Unverified summary:** the summary is never checked by the validator, and is labelled as such.
- **Images:** every image is a placeholder, because the catalogue URLs are fake.
- **Narrow preference detection:** only capitalised brand names are detected.
- **No aspect analysis:** Phase 2 was never implemented.
- **Per-tab UI state:** lost on reload.
- **Latency:** about 6–8 s per answer turn in fallback mode, above the ~3 s spec target.
- **Carried over from Phase 8:** the unused `min_rating` preference, limited category diversity, no per-user authentication.

## 9. Next Phase

Per `docs/master-spec.md` §16: **Phase 10, Evaluation and comparison** (labelled query set, metrics, variants A–D, latency and memory).

**Ollama status (checked 2026-10-19):** not installed (no `ollama` binary; nothing on `127.0.0.1:11434`). Every answer in the current environment is an offline fallback, marked `generation.mode = "fallback"` and labelled in the UI. Phase 10's LLM variants need the model installed.

*Phase 9 is complete. Waiting for your own inspection of the website before Phase 10.*

---

## 10. Addendum: Relevance Fix and Frontend Redesign (after your inspection, approved plan)

**Reported issue.** "I need a shoe under 4000 rupees, which can be everyday use, sneakers." returned a power bank, a laptop stand, running shoes at USD 89.99, and other electronics.

**Root causes (traced in code and data):**
1. No product-type constraint existed. The rule-based parser never set one, and catalogue categories are too coarse.
2. The keyword query kept stopwords: "a" matched "USB-A", making the power bank the only keyword hit. "shoe" didn't match "Shoes".
3. The offline "semantic" index is 64-d feature hashing, and RRF always filled 10 slots.
4. "rupees" was dropped, so 4000 was compared directly with USD prices.
5. Data: 20 hand-made fixture products, all USD, one shoe, no sneakers. No licensed dataset was ever ingested.

There were also two latent bugs: an LLM-parsed `category: "shoes"` would have filtered out the real shoe, and parsed exclusions were never enforced.

**Changes (approved decisions: optional `constraints` field plus per-card checks; additive `MetadataFilter` fields and a new keyword sanitiser; curated type list; currency mismatch → clarify with no cards):**
- **Retrieval**
  - `src/retrieval/constraints.py` (new): type list, currency detection, word matching.
  - `filters.py`: additive `product_types`, `excluded_brands`, `excluded_terms`, `price_currency`, enforced in `matches()` and the SQL currency clause.
  - `keyword.py`: stopwords, plural prefixes, over-fetch then filter.
  - `semantic.py`: whole-index scan when constrained.
  - `hybrid.py`: type-synonym expansion, plus the relevance floor (`semantic_only_results=False`).
- **Agent**
  - `src/agent/constraints.py` (new): resolver, report, clarification, per-card checks.
  - `query_parser.py`: currency-aware price parsing, deterministic constraints on both paths, constraint phrases removed from search text.
  - `schemas.py`: `ConstraintReport`, `ConstraintCheck`, `ParsedQuery.price_currency` / `product_types`.
  - `session.py`: refine merging.
  - `intent_router.py`, `controller.py`, `presentation.py`.
- **Prompts:** `src/llm/prompts.py` and `service.py` showed **hard-coded `$`** for budgets and product prices. They now use real currency codes. The validator still parses `USD 49.99`, and `src/evidence/` is unchanged.
- **Frontend:**
  - `frontend/theme.py` (new): palette, contrast check, scoped CSS; `.streamlit/config.toml` theme.
  - Empty-state hero with a search form and examples.
  - "Your request" panel with ✓ / ⚠ chips; currency on every price and chip; "Meets your requirements" on cards.
  - Tabbed "Why this result?".
  - Sidebar status, preference editor, feedback list.
- **Unchanged:** database schema, `EvidenceValidator` and thresholds, security controls, consent, wishlist, feedback, export and delete.

**Tests.** 71 new or extended tests:
- `tests/unit/test_constraints.py` (47), including **240 seeded random constraint sets × 4 retrieval modes**: no result ever violates an applied constraint.
- `tests/integration/test_relevance_api.py` (11): your exact query end-to-end via `/chat` on the real catalogue, plus variants, combinations, no-match, refinement, and validation still running.
- UI: +7 view-model and theme tests (incl. WCAG AA contrast), +7 headless app tests.
- 7 existing tests updated to the intended new behaviour: FTS token format, no invented `$`, response key sets, budget kept out of search text, inferred USD in a filter, chip format, and the form-submit test interaction. Each still asserts what it protected.

**Results:**
- Full suite, 3 consecutive runs: each **563 passed, 0 failed, 1 skipped**. The skip is the live-Ollama test, because Ollama isn't installed.
- Phase 4 retrieval benchmark: **unchanged**. Keyword, hybrid and reranked are all 1.0; semantic has Recall@1 0.80 and MRR 0.8917.

**Live check** (uvicorn and Streamlit on 127.0.0.1, fresh database copy, Ollama absent):

| Query | Result |
|---|---|
| Your rupee query | **Clarification:** budget in INR, catalogue in USD, no exchange-rate data, 1 product matches the rest. No products, no AI answer |
| Same request "under $100" | Only *Marathon Running Shoes - Men*, USD 89.99. Card checks: "footwear: title mentions “shoe”", "USD 89.99 ≤ USD 100.00". Fallback answer, support 0.67, flagged not valid |
| "sneakers under $50" | Honest no-match, 0 matching products |
| "shoe under 4000" | Read as USD 4,000, and labelled as an assumption |

The redesigned UI rendered all of these through the real client with no exceptions.

**Not verified.** The visual design in a real browser (layout, narrow screens, keyboard flow, screen reader) is for your inspection; I have not seen it rendered.

**Remaining limits.** These are in `limitations.md` and `data-sources.md`:
- sample data only, USD only;
- lexical product types ("laptop" matches *Laptop Stand*);
- weak offline ranking;
- mention-based exclusions;
- a bare "under $50" starts a new search.
