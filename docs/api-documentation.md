# API Documentation

Self Search backend: FastAPI app in `src/api/app.py` (version `0.2.0`).

Run locally:

```
.\venv\Scripts\python.exe -m uvicorn src.api.app:app --reload
```

Interactive OpenAPI docs are served at `/docs` once running.

## Authentication

Optional. If the `API_KEY` environment variable is set, every endpoint except `/health` requires the header `X-API-Key: <value>`. A missing or wrong key returns `401`. If `API_KEY` is unset, no key is required.

There are **no user accounts or profiles** (removed on 2026-10-21). The API key is one shared secret, compared in constant time. See [deployment.md](deployment.md).

---

## POST /chat

Conversational product discovery (Phase 7). Each message is parsed and routed by intent.

- **Searches and refinements are product-first (2026-10-22).** They apply every hard constraint, retrieve, and return the products as soon as retrieval finishes, **without calling the model**. `answer` is then a claim-free placeholder (`summary: ""`, `recommendations: []`), still passed through `EvidenceValidator`, and `generation` is `{"mode": "none", "reason": "summary_not_requested"}`. The optional AI summary of these results is `POST /chat/summary`.
- **Questions about a product and comparisons** run retrieval → LLM → `EvidenceValidator`, as before.

**Request**

| Field | Type | Required | Notes |
|---|---|---|---|
| `message` | string | yes | Minimum length 1. An empty string returns `422`. |
| `session_id` | string | no | Omit to start a new session. An unknown ID creates a session with that ID. |
| `product_ids` | int[] | no | *Phase 9.* Explicit product selection from the UI. One ID → question about that product (`PRODUCT_QUESTION`); 2–4 IDs → `COMPARE`. Every ID must have been shown in this session; otherwise `422`, with no change to session state. Smalltalk and out-of-scope messages are not overridden. |

```json
{"message": "wireless headphones under $100", "session_id": null}
```

Requests without `product_ids` behave exactly as before Phase 9. A `user_id` field sent by an older client is ignored.

**Response** (`AgentResponse`, `src/agent/schemas.py`)

| Field | Type | Present when |
|---|---|---|
| `session_id` | string | always; send it back on the next turn |
| `intent` | enum | always: `PRODUCT_SEARCH`, `REFINE_SEARCH`, `PRODUCT_QUESTION`, `COMPARE`, `CLARIFY`, `SMALLTALK`, `OUT_OF_SCOPE` |
| `answer` | object \| null | search/refine/question/compare: serialised `StructuredAnswer` (`summary`, `recommendations[].why_it_matches[].evidence_ids`, `insufficient_evidence`, …) **after** evidence validation |
| `support_report` | object \| null | whenever `answer` is present: `SupportReport.to_dict()` (`support_rate`, `fabricated_citations`, `numeric_mismatches`, `is_valid`, per-claim results) |
| `clarification_question` | string \| null | `CLARIFY`, or when a follow-up cannot be resolved safely (e.g. "is it waterproof?" after several results) |
| `smalltalk_response` | string \| null | `SMALLTALK`, `OUT_OF_SCOPE` |
| `active_filters` | object | always: `category`, `brand`, `min_price`, `max_price`, `min_rating` |
| `shown_product_ids` | int[] | always: every product shown in this session |
| `parsing_confidence` | float | always: 0–1. Rule-based fallback reports 0.4 |
| `result_product_ids` | int[] | *Phase 9.* Products this answer is about, in display order. For a search: retrieval order (no recommendations yet). For a question or comparison: recommended products first (in recommendation order), then the other candidates. A recommendation for a product that was not a candidate is ignored. `[]` on turns without an answer. |
| `products` | ProductCard[] | *Phase 9.* **Database records** for `result_product_ids`, same order: `id, external_id, title, brand, category, price, currency, catalog_rating, catalog_rating_count, local_review_count, image_url, features, attributes, aspects, recommended, retrieval_rank`. Never taken from LLM output. `catalog_rating*` are the source dataset's figures; `local_review_count` is computed from the local `reviews` table. `aspects` is `[]` while review intelligence (Phase 2) is unimplemented. |
| `evidence` | EvidenceSnippet[] | *Phase 9.* Every item of the evidence pack passed to `EvidenceValidator` for this answer (`evidence_id, review_id, product_id, title, text, rating, verified, helpful_votes`). Cited IDs missing from this list did not exist in the evidence. |
| `generation` | object \| null | *Phase 9.* How the answer was actually produced, decided where it happens: `{"mode": "llm", "model", "attempts"}`, `{"mode": "fallback", "reason": "provider_unavailable" \| "provider_error" \| "invalid_output_after_retries" \| "unexpected_error"}` (deterministic template; still validated), `{"mode": "none", "reason": "no_products_retrieved"}`, or `{"mode": "none", "reason": "summary_not_requested"}` (a product-first search). `null` without an answer. |

| `constraints` | object \| null | *Phase 9 relevance fix.* For search and refine turns, including a currency clarification: `{items: [{kind, label, value, status: "applied"\|"not_applied", reason, checked_fields}], matching_products, catalog_currencies}`. `kind` ∈ `product_type, category, brand, price, min_rating, excluded_brand, excluded_term, colour, material, occasion`. A `price` item's `value` has `{min, max, currency, currency_source: "stated"\|"catalog_default"\|"earlier_turn"}`. `matching_products` = catalogue products meeting **every applied** constraint. Every product in `products` meets all applied constraints. `null` on other turns. |

Each `products[]` card also has `constraint_checks: [{kind, detail}]`, e.g. `{"kind": "price", "detail": "₹7,477 ≤ ₹8,000 (converted from the source price at ₹83.08 per US dollar (ECB reference rate, 2023-09-29))"}`, `{"kind": "product_type", "detail": "footwear: title mentions “shoe”"}`. It is computed from the product's database fields, and is empty for question and compare turns. `kind` may also be `colour`, `material` or `occasion` (2026-10-23).

**Every price is INR (2026-10-23).** Cards carry no amount in any other currency. The fields are:
- `price`, `currency`: the source price **in INR**, `currency` `"INR"`, or both `null` (price unavailable).
  - An INR source price is passed through as supplied.
  - A source price in another currency is converted with the documented rate in `config/exchange_rates.toml`: an official source (ECB reference rates) for a stated date, whole rupees, rounded half up.
  - The original amount stays in the database (`products.price`/`currency`, and `price_normalization` in expanded catalogues) for traceability and is never sent to clients.
  - A currency without a documented rate, or a source price of zero or less, gives `null`.
- `price_status`:
  - `"verified"`: the source price was in INR;
  - `"converted"`: the source price was in another currency and is converted at the documented rate;
  - `"estimated"`: no source price; a synthetic INR estimate is shown;
  - `"unknown"`: no usable price ("Price unavailable").
- `price_conversion` (only for `converted`): `{from_currency, rate, rate_date, source, source_url, fx_version, label}`. It never contains the original amount.
- `estimated_price`: `{amount, currency: "INR", range_min, range_max, rule, config_version, label: "Synthetic estimate, not a retailer price"}`, or `null`. `price` is never an estimate.
- `image_url` and `image_alternates`: the validated main photo of this product, plus other validated photos of the same product to try if it fails (2026-10-23).

**Budgets are INR.** A bound is applied to each product's INR price (verified or converted) and, when estimates are enabled (`SHELFSENSE_ESTIMATED_PRICES_ENABLED=true`, default `false`), for products **without a source price** only, to the estimate. Unknown prices never meet a budget.
- A budget without a currency is read as rupees (`currency_source: "catalog_default"`).
- The price item's `label` reads, for example, "Max price: ₹3,000 (prices in ₹, including source prices converted at a documented exchange rate; unknown prices never match)". With estimates it also has `value.uses_synthetic_estimates: true`.
- `catalog_currencies` is `["INR"]`.

**Budget in another currency.** A budget stated in $, €, £ (or USD/EUR/GBP) is not applied and not converted. The response has `clarification_question` set (asking for the budget in ₹), `answer: null` and no products. The price item is `status: "not_applied"`, labelled "Budget not in rupees", with the reason.

Estimates never enter LLM prompts, fallback answers, evidence packs or validation. Prompts carry only the INR price, or "unavailable", with the conversion's provenance, never the original amount. A foreign-currency amount the model writes counts as a numeric mismatch and is removed from the answer text, the claims and `support_report`.

`answer` itself is unchanged and still contains the LLM's own `recommendations[].title`; **display product facts from `products`, not from `answer`.** The answer's `summary` is not checked by the validator, which validates only claims.

The intent can resolve to a clarification. For example, `intent: PRODUCT_QUESTION` with `answer: null` and a `clarification_question` means the agent could not tell which product was meant.

**Errors**:
- `422`: invalid body, or invalid `product_ids` (empty, more than 4, duplicates, no existing session, or not shown in this session)
- `401`: bad API key
- `500`: unexpected server error

Routing errors inside the agent do not produce a `500`; they degrade to a clarification response.

**Example conversation**

```
POST /chat {"message": "wireless headphones under $100"}
  → intent PRODUCT_SEARCH, active_filters.max_price 100, products (no model call)
POST /chat/summary {"session_id": "<id>", "result_product_ids": [...]}
  → the same products in the same order, with the validated AI summary + support_report
POST /chat {"message": "only 4+ stars", "session_id": "<id>"}
  → intent REFINE_SEARCH, filters max_price 100 + min_rating 4.0
POST /chat {"message": "is it waterproof?", "session_id": "<id>"}
  → intent PRODUCT_QUESTION, answer null, clarification_question "I showed 3 products. Which one…"
POST /chat {"message": "is the second one waterproof?", "session_id": "<id>"}
  → intent PRODUCT_QUESTION, answer about result #2 + support_report
POST /chat {"message": "compare these two", "session_id": "<id>"}
  → intent COMPARE, answer comparing results #1 and #2 + support_report
```

## POST /chat/summary

*Added 2026-10-22.* The optional, evidence-grounded AI summary of the session's **latest search results**. It is the only way a search gets a model summary.

**Request**

| Field | Type | Required | Notes |
|---|---|---|---|
| `session_id` | string | yes | A session that has run a search. |
| `result_product_ids` | int[] | no | The `result_product_ids` the client is showing. If given, they must equal the session's latest results; otherwise `409`. This way a summary never describes a different result set. |

**What it does:**
- It covers the **top 3** results, each with its own review evidence (at most 2 snippets per product, an 800-token evidence budget).
- The answer goes through `EvidenceValidator` like every other answer: fabricated citations, cross-product citations and numeric mismatches are flagged. Synthetic price estimates are never sent to the model.
- **Response:** an `AgentResponse` for the same search. It has the **same `products` in the same order** as the search, now with `answer`, `support_report`, `evidence` and `generation`. Recommended cards have `recommended: true`, but they are not moved. `constraints` and `active_filters` are those of the search.
- **One model call per result set:**
  - Requests for one session are handled one at a time.
  - A summary the model wrote is cached for that result set, so a repeated request returns it without calling the model.
  - A `fallback` summary (the model was unavailable or failed) is **not** cached, so the client can retry.
- The model budget is the same 110 s per request (`SHELFSENSE_OLLAMA_TIMEOUT`).

**Errors:**
- `404`: unknown session
- `409`: the session has no search results, or `result_product_ids` are no longer the latest results
- `401`: bad API key
- `422`: invalid body
- `500`: unexpected server error

## GET /sessions/{session_id}

Returns session state: `session_id`, `active_filters`, `shown_product_ids`, `conversation_turn_count`, `last_intent`, `created_at`. Unknown ID → `404`.

## DELETE /sessions/{session_id}

Deletes a session. Returns `{"status": "deleted", "session_id": "..."}`. Unknown ID → `404`.

## DELETE /sessions/{session_id}/filters/{name}

*Phase 9.* Removes one active filter from the in-memory session; it applies to the next search or refinement. `name` ∈ `category, brand, min_price, max_price, min_rating` (other names → `422`). Returns `{"session_id", "active_filters"}`. Unknown session → `404`. Nothing is persisted.

## GET /products/{product_id}

*Phase 9.* Read-only product detail from the database: `{"product": ProductCard, "reviews": [{review_id, rating, title, text, review_date, helpful_votes, verified}]}`. Reviews are the locally stored ones, most helpful first, up to 20. Unknown ID → `404`. API key required if configured.

## GET /health

No authentication. Returns:

```json
{"status": "ok", "database": "connected", "ollama": "available", "active_sessions": 2}
```

`ollama` is `unavailable` when the local daemon is not running. The agent still works in that case, using the rule-based parser and the Phase 5 safe-fallback answers.

---

## Removed: personal memory (`/users`)

User profiles, consent, preferences, wishlist, feedback, export and profile deletion were removed on 2026-10-21, together with the `user_id` request field, the `personalization` and `preference_suggestions` response fields and the `PERSONAL` intent. Every `/users/...` path now returns `404`. The database tables are left in place until a separate, approved cleanup (see [database-schema.md](database-schema.md)).

---

## Legacy endpoints (pre-Phase 7, kept for compatibility)

| Endpoint | Purpose |
|---|---|
| `POST /ask` | `{"query": str, "k": 1–20}` → `{"answer": str, "evidence": [{entity_type, entity_id, distance}]}`. Not evidence-validated; use `/chat`. |
| `POST /populate` | Regenerates embeddings. Returns `{"status": ...}`. |

## Sessions

Sessions are in memory and are lost when the server restarts. Their contents (filters, shown products, conversation turns) are never written to the database.
