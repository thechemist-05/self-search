# Phase 7 Report: Agent and Query Understanding

**Date:** 2026-10-19
**Status:** Complete, pending approval. 267 passed, 1 skipped (Ollama offline), 0 failed.

---

## 1. What Was Built

Phase 7 adds the conversational layer (spec §11, §16 Phase 7; requirements F1, F8, F9, F12):

| Spec task | Implementation |
|---|---|
| Query parser (constraints → structured filters) | `QueryParser` (`src/agent/query_parser.py`). The primary path is the LLM with schema validation, 1 retry and output sanitisation. The fallback is a deterministic rule-based parser. Output is `ParsedQuery`: intent, `search_text`, category, brand, min/max price, min rating, preferences, exclusions, compare targets, confidence. |
| Intent router | `IntentRouter` (`src/agent/intent_router.py`), covering all 7 intents from spec §11 |
| Session state (active filters, shown products) | `SessionState` / `SessionManager` (`src/agent/session.py`): filter merge on refine, shown and presented products, focused product, accumulated search text, sliding-window history |
| Clarification logic | LLM confidence < 0.5 → `CLARIFY`; offline vague-query detection; ambiguous follow-ups ask "which one?" instead of guessing |
| Refine and compare flows | Refine merges filters and keeps the original search text. Compare resolves "these two", "#1 and #3", or the top results (2–4 products). |
| Out-of-scope handling | LLM intent, plus an offline pattern list (weather, jokes, code-writing, prompt-injection phrases) |
| FastAPI endpoints | `POST /chat`, `GET/DELETE /sessions/{id}`, `GET /health` in `src/api/app.py`. Legacy `/ask` and `/populate` are unchanged. |

**Evidence path.** Every answer-generating intent (`PRODUCT_SEARCH`, `REFINE_SEARCH`, `PRODUCT_QUESTION`, `COMPARE`) calls `LLMService.generate_answer` and then `EvidenceValidator.validate_answer` before anything is returned. `CLARIFY`, `SMALLTALK` and `OUT_OF_SCOPE` generate no product claims and never call retrieval or the LLM answer path. The query parser never touches the catalogue, and its preferences and exclusions are search signals, not product facts.

## 2. Issues Found and Fixed While Completing the Phase

The first Phase 7 pass stopped before reporting. Its tests (215) all passed but were mock-heavy. Review and a real offline smoke run found the following, all inside Phase 7 scope:

| # | Issue | Impact | Fix |
|---|---|---|---|
| 1 | `create_default_agent_controller` called `LLMService(llm_provider=…)`; the parameter is `provider` | `/chat` crashed with `TypeError` on first use in production. Tests injected a mock controller, so this was missed. | Corrected the kwarg; added `test_default_controller_wiring` |
| 2 | The shared SQLite connection was thread-bound, but FastAPI runs sync endpoints in a threadpool | Every retrieval turn raised `ProgrammingError`, which surfaced as "I encountered an issue…" with HTTP 200 | `open_api_connection()` in `app.py` uses `check_same_thread=False` (the agent only reads); added `test_api_connection_usable_from_worker_threads`. `src/db/connection.py` is unchanged. |
| 3 | The offline parser had no vague-query or out-of-scope detection | With Ollama down (the default here), "tell me a joke" and injection prompts went into product retrieval. 22/28 labelled intents were correct. | Added out-of-scope patterns and vague detection; now 28/28 |
| 4 | No offline exclusion extraction; "without RGB" kept "RGB" in the keyword query | Retrieval boosted the products the user wanted to avoid | Rule-based exclusions, stripped from `search_text` |
| 5 | Questions targeted `shown_product_ids[-1]` | "Is it waterproof?" after a search answered about the **lowest-ranked** result | Positional resolution against the presented results; asks when ambiguous |
| 6 | Compare used `shown_product_ids[-4:]` | "Compare these two" compared the 4 *lowest-ranked* products | Count and position resolution against the presented results |
| 7 | Refine replaced the search text with the refinement | "only black ones" searched for "only black ones" and lost "headphones" | `active_search_text` is accumulated across refinements |
| 8 | LLM-parsed constraints were used unchecked; the prompt told the model to *estimate* a budget for "cheap" | The model could invent a price filter, an impossible rating, or catalogue IDs it never saw | Prompt fixed; `_sanitize_llm_constraints` drops or repairs bad values; compare IDs not shown in the session are dropped |
| 9 | `rule-based` regex treated "show me headphones" (with history) as a refinement | A new search was merged into the old filters | Tightened the refine pattern |

## 3. Files Created and Modified

No git repository exists, so this list comes from file timestamps and inspection.

**Source (created in Phase 7):** `src/agent/schemas.py`, `src/agent/prompts.py`, `src/agent/query_parser.py`, `src/agent/session.py`, `src/agent/intent_router.py`, `src/agent/controller.py`, `src/agent/__init__.py`
**Source (modified):** `src/api/app.py`: Phase 7 endpoints, controller wiring, and fixes 1–2

**Tests:** `tests/fixtures/query_parsing_cases.json` (28 labelled queries), `tests/unit/test_query_parser.py`, `tests/unit/test_intent_router.py`, `tests/unit/test_session.py`, `tests/unit/test_agent_controller.py`, `tests/integration/test_agent_api.py`

**Docs:** `docs/agent-design.md`, `docs/api-documentation.md` (was a stub), `docs/phase-reports/phase-7-report.md`

There are no schema, dependency, or database changes. `AgentResponse` and `ChatRequest` are unchanged from the first Phase 7 pass. `ParsedQuery` is unchanged. `SessionState` gained three defaulted fields and `set_search_results()`.

## 4. Tests

Command (project venv, Python 3.14.0, pytest 8.4.2):

```
.\venv\Scripts\python.exe -m pytest tests/unit tests/integration -v
```

Result:

```
267 passed, 1 skipped, 34 warnings in 22.42s
SKIPPED [1] tests/integration/test_llm_ollama.py:21: Local Ollama daemon is not running
```

| Phase 7 test file | Tests |
|---|---|
| `tests/unit/test_query_parser.py` | 57 |
| `tests/unit/test_intent_router.py` | 18 |
| `tests/unit/test_session.py` | 17 |
| `tests/unit/test_agent_controller.py` | 4 |
| `tests/integration/test_agent_api.py` | 12 |
| **Phase 7 total** | **108** (was 56) |

The skip is an environment condition, not a failure. The 34 warnings are pre-existing deprecations (`datetime.utcnow` in `src/data/source_registry.py`, Starlette's `httpx` notice). All Phase 7 tests are deterministic and need neither network nor Ollama: the LLM is `MockLLMProvider`, and retrieval, generation and validation are mocked or stubbed.

Coverage against the spec's Phase 7 test list:
- **Intent and constraint extraction on a labelled query list:** all 28 fixture intents, plus every labelled price, rating and exclusion, for the offline parser.
- **Multi-turn refinement scenarios:** router tests, plus `test_offline_multi_turn_conversation`. That test runs search → refine → ambiguous question → positional question → compare → out-of-scope → vague over `/chat` with the real parser, router and session, and asserts the validator ran on all 4 answer turns.
- **API tests:** `/chat`, session get/delete and 404s, `/health`, `422` on an empty message, API key, production wiring, threadpool safety.

**Real-data smoke run (manual).** A copy of `data/processed/shelfsense.db` (20 products, 100 reviews) was run with the real `HybridRetriever`, `LLMService` and `EvidenceValidator`, with `HF_HUB_OFFLINE=1` and Ollama down, via `TestClient`. All 8 turns returned 200 with correct intents, accumulated filters (`max_price 100` + `min_rating 4.0`), correct follow-up targets, and a `support_report` on every answer turn.

## 5. Exit Criterion

> End-to-end conversation works via API (no UI yet).

**Met.** It was verified through FastAPI's `TestClient` and, after approval, on a live uvicorn server. It was not verified with a live Ollama model, since the daemon is not running.

**Live-server verification (2026-10-19, post-acceptance).** `uvicorn[standard]` was installed into `venv` exactly as declared in `requirements.txt`: uvicorn 0.54.0, plus httptools, watchfiles and websockets from the extra. `requirements.txt` is unchanged. The app ran via `python -m uvicorn src.api.app:app --port 8765` against a copy of the database, with `HF_HUB_OFFLINE=1` and Ollama down:

| Request | Result |
|---|---|
| `GET /health` | 200: database connected, ollama unavailable |
| `POST /chat` "wireless earbuds under $100" | 200, 5.72 s: `PRODUCT_SEARCH`, `max_price 100`, recs [2, 10, 12], `support_rate 0.5556`, `is_valid false`, 4/6 claims flagged `[UNVERIFIED]` |
| `POST /chat` "is it comfortable?" | 200, 1.05 s: `PRODUCT_QUESTION` → clarification "I showed 3 products. Which one…" (no answer generated) |
| `POST /chat` "is the first one comfortable?" | 200, 5.09 s: `PRODUCT_QUESTION` on product 2, `support_rate 0.40`, `is_valid false`, 2/4 claims flagged |
| `GET` / `DELETE` / `GET /sessions/{id}` | 200 / 200 / 404 |
| `POST /chat` with an empty message | 422 |

The server log showed no exceptions. Both answer turns went through `EvidenceValidator`. Latency on this laptop, about 5 s per answer turn with no LLM call, is above the spec's ~3 s target for cards and is recorded for Phase 10/11.

## 6. Known Limitations

1. **Offline answers have low support rates.** With Ollama down, the Phase 5 safe-fallback template answers scored `support_rate` 0.33–0.56 (`is_valid: false`) in the smoke run, and the validator correctly flags them. This is Phase 5/6 behaviour, reported here because it is what a user sees offline.
2. The LLM parser path has been tested only with mocked outputs. Its accuracy on the labelled set with a real model is unmeasured (Phase 10).
3. Exclusions and preferences are extracted but not enforced as filters. "Cheaper" does not compute a new price bound. The offline parser does not set category or brand filters. See `docs/agent-design.md` §7.
4. Sessions are in memory only. `SessionManager` has no locking: concurrent requests on the *same* session are not serialised.
5. `AgentResponse` has no per-turn product records (title, price, image) or "products in this answer" list. Cards in Phase 9 can use `answer.recommendations[].product_id` but would need a lookup.

## 7. Decisions for You

1. ~~Install `uvicorn` into `venv`?~~ Approved and done (Section 5).
2. **`AgentResponse` additions** (limitation 5): **deferred to Phase 9 planning** by decision. The `/chat` contract is unchanged.

## 8. Next Phase

Per `docs/master-spec.md` §16: **Phase 8, Personal memory.** Scope: users, preferences, wishlist and feedback services; personalised re-ranking; personal RAG ("like what I saved"); export and delete-my-data.

*Phase 7 is complete. Do you approve moving to Phase 8?*
