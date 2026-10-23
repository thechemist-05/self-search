# Self Search Agent Architecture & Query Understanding (Phase 7)

## 1. Overview

Phase 7 introduces the conversational and reasoning layer of Self Search. The agent acts as the conversational orchestrator connecting user natural language intent with:
1. Structured constraint extraction (query understanding)
2. Hybrid candidate retrieval (keyword + semantic + RRF)
3. Evidence pack assembly (reviews + product specs)
4. LLM structured generation
5. Grounded evidence validation (checking citations, numeric figures, and claim grounding)
6. Ephemeral multi-turn session state management

```
User Message
     │
     ▼
┌─────────────────────────┐
│ FastAPI (/chat)         │
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│ AgentController         │
└──────┬───────────┬──────┘
       │           │
       ▼           ▼
┌──────────────┐ ┌──────────────┐
│ QueryParser  │ │ SessionState │ (Tracks active filters, shown products,
└──────┬───────┘ └──────────────┘  sliding window conversation turns)
       │
       ▼
┌─────────────────────────┐
│ IntentRouter            │
└──────┬──────────────────┘
       │
       ├─────────────────────────┬──────────────────────────┐
       ▼                         ▼                          ▼
 [SEARCH / REFINE]          [CLARIFY]                  [SMALLTALK /
  HybridRetriever                │                      OUT_OF_SCOPE]
        │                   Returns focused                  │
        ▼                   clarification              Returns friendly
  LLMService                question without           canned guidance
        │                   retrieval
        ▼
  EvidenceValidator
        │
        ▼
 AgentResponse (Validated Answer + SupportReport + Active Filters)
```

---

## 2. Core Separation: Query Understanding vs. Factual Generation

A foundational principle in Self Search is strict separation of concerns:
- **`ParsedQuery` (src/agent/schemas.py)** represents *inferred user intent and search signals*.
  - Extracted constraints: `category`, `brand`, `min_price`, `max_price`, `min_rating`, `preferences`, `exclusions`.
  - Inferred preferences (e.g. `["noise-cancelling", "wireless"]`) and exclusions (e.g. `["no wired"]`) are **never treated as verified product attributes**.
  - They guide the search engine, but are never asserted as product facts to the user.
- **`StructuredAnswer` (src/llm/schemas.py)** represents *factual product recommendations*.
  - Every claim must reference retrieved review evidence IDs (`evidence_ids`).
  - Claims and numeric values are strictly validated by `EvidenceValidator`.

---

## 3. Query Parsing (`src/agent/query_parser.py`)

The `QueryParser` combines two complementary parsing strategies. **Which one runs is set by `SHELFSENSE_QUERY_PARSER_MODE`** (2026-10-21):

- **`rules_first` (default).** The deterministic rule-based parser (2. below) handles every message it can safely handle, with **no model call**. The LLM parser is used only when the message has a *brand cue* (`from X`, `by X`, `brand`, `branded`), because the rule-based parser never extracts a brand and a hard brand filter would otherwise be lost. Price phrases such as "from 500 to 1000" or "by $20" are not brand cues. If the model is unavailable or fails, the rule-based parse is used. On a CPU-only machine this saves one model call per turn: about 41 s with `llama3.2:3b` on the Fashion catalogue.
- **`llm_first`.** The previous behaviour: the LLM parses every message, and the rules are the fallback.

Retrieval, filters, evidence validation (including numeric checks) and the answer fallback are identical in both modes. Only the step that turns the message into a `ParsedQuery` differs.

1. **LLM-Based Parser** (every message in `llm_first`; brand-cue messages in `rules_first`):
   - Uses a dedicated query understanding prompt (`src/agent/prompts.py`).
   - Requests structured JSON conforming to `ParsedQuery`.
   - Employs self-assessed confidence scoring (0.0 to 1.0).
   - If confidence is `< 0.5`, the intent is automatically shifted to `CLARIFY` to avoid hallucinations.
   - Retries once on invalid JSON before falling back.
   - **Output sanitisation** (`_sanitize_llm_constraints`). A schema-valid parse can still be wrong, so before use:
     - ratings outside (0, 5] are dropped;
     - negative prices are dropped;
     - any price is dropped when the user message contains no digits (the model invented it, e.g. "cheap" → $50);
     - an inverted range (min > max) is swapped.
   - Non-object JSON, unknown intent values, or provider exceptions all fall back to the rule-based parser.

2. **Deterministic Rule-Based Parser** (default path in `rules_first`; fallback in both modes):
   - Activates for every message without a brand cue in `rules_first` mode, and whenever the LLM is unreachable or fails schema validation. It is also the path used whenever Ollama is not running, so it must handle F12 on its own.
   - Deterministic regular expressions extract:
     - Price upper bounds: `under $100`, `below 3000`, `less than 50`
     - Price lower bounds: `above $50`, `over 200`, `starting at $500`
     - Price ranges: `between $50 and $100`
     - Ratings: `4.5+ stars`, `rated above 4.0`, `top rated` (defaults to 4.0)
     - Exclusions: `without RGB lights`, `not Razer`, `no wired options` → `["RGB lights", "Razer", "wired"]`. Exclusion clauses are removed from `search_text` so keyword retrieval does not boost the excluded term.
   - Intent order: `SMALLTALK` → `OUT_OF_SCOPE` → `COMPARE` → `PRODUCT_QUESTION` (with history) → `REFINE_SEARCH` (with history) → `CLARIFY` (vague) → `PRODUCT_SEARCH`.
     - `OUT_OF_SCOPE` uses a deliberately narrow pattern list (weather, jokes, code/poem writing, "ignore previous instructions", …). Product phrases such as "all weather jacket" or "password manager" are not matched.
     - `CLARIFY` fires when every word is non-specific ("I want something nice", "help me buy stuff") and no price or rating constraint is present.
   - Category and brand are **not** extracted offline. Those words remain in `search_text` and still drive semantic and keyword retrieval, but they do not become metadata filters.
   - Accuracy on the 28 labelled cases in `tests/fixtures/query_parsing_cases.json`: 28/28 intents, and every labelled price, rating and exclusion value. This is enforced by `test_rule_based_intent_matches_every_labelled_case`.

---

## 4. Session State Management (`src/agent/session.py`)

- **In-Memory Tracking**: Sessions are maintained in `SessionManager` indexed by UUID.
- **Filter Accumulation**:
  - `PRODUCT_SEARCH`: Replaces active filters with a clean slate.
  - `REFINE_SEARCH`: Merges delta constraints into existing `MetadataFilter` (e.g., maintaining category="headphones" while adding max_price=100.0 and brand="Sony").
  - Search context: `active_search_text` stores the retrieval text of the last search. A refinement is retrieved as `active_search_text + refinement`, so "only black ones" still searches for "wireless headphones only black ones".
- **Shown Products Tracking**:
  - `shown_product_ids`: every product shown in the session (deduplicated, first-seen order). Used to validate compare targets.
  - `last_result_ids`: the products the most recent search or refine *presented*, in the order shown. This is the validated answer's recommendations, restricted to retrieved IDs, or the retrieval ranking if there are no recommendations. Follow-ups resolve against this list, so "the first one" means the first product the user saw.
  - `focused_product_id`: the product the user last asked a question about, so "and does it run small?" keeps the same target. It is reset by every new search.
- **Sliding Window History**:
  - Automatically bounds memory to the last $N$ turns (default 20), avoiding unbounded memory growth.

---

## 5. Intent Routing (`src/agent/intent_router.py`)

| User Intent | Handler | Workflow | Validation Pipeline |
|---|---|---|---|
| `PRODUCT_SEARCH` | `_handle_search` | Hard constraints → `HybridRetriever` → claim-free answer → `EvidenceValidator`. **No model call** (product-first, 2026-10-22). Records a `SearchSnapshot` of the results on the session. | Mandatory |
| `REFINE_SEARCH` | `_handle_refine` | Merges filters onto session → `_handle_search` | Mandatory |
| *(optional summary)* | `summarise` | Only via `AgentController.summarise_results` (`POST /chat/summary`): the snapshot's top 3 products → their evidence pack → `LLMService` → `EvidenceValidator`. Cards keep their order. | Mandatory |
| `PRODUCT_QUESTION` | `_handle_question` | Retrieves reviews for target product → `LLMService` → `EvidenceValidator` | Mandatory |
| `COMPARE` | `_handle_compare` | Loads candidate products (2–4) → combined evidence pack → `LLMService` → `EvidenceValidator` | Mandatory |
| `CLARIFY` | `_handle_clarify` | Returns clarifying question | None (no claims generated) |
| `SMALLTALK` | `_handle_smalltalk` | Context-appropriate greeting / thanks / farewell | None (no claims generated) |
| `OUT_OF_SCOPE` | `_handle_out_of_scope` | Polite steering back to product discovery | None (no claims generated) |

### 5.1 Follow-up reference resolution

The parser never sees catalog IDs, so references are resolved deterministically in the router against `last_result_ids`. Helpers: `extract_ordinal_references` and `extract_compare_count` in `query_parser.py`.

**`PRODUCT_QUESTION`**, in priority order:
1. An explicit position: "is the **second one** waterproof?", "#2", "product 3", "the last one".
2. Otherwise `focused_product_id`.
3. Otherwise the single result, if only one was shown.
4. Otherwise ask: *"I showed 3 products. Which one are you asking about? …"* The agent does not guess.

An out-of-range position ("the fifth one" when two were shown) also produces a clarification.

**`COMPARE`**, in priority order:
1. A count: "these **two**", "top 3", "both" → the top-N of the latest results.
2. Positions: "#1 and #3".
3. `compare_product_ids` from the LLM parser, accepted **only if those IDs were actually shown** in this session. Other IDs are dropped and logged as `compare_ids_not_shown_dropped`.
4. Otherwise the top 4 of the latest results.

Fewer than 2 resolved products → clarification. Products are passed to the LLM in the order the user referred to them.

Since searches are product-first, "the first one" is always the first card shown. An optional summary never reorders the cards, so positions stay valid after it.

### 5.1a Optional summary of a search's results (2026-10-22)

**`SearchSnapshot`** (`src/agent/session.py`) records what the latest search showed: its intent, search text, filters, product ids and constraint report. `AgentController.summarise_results(session_id, result_product_ids)` summarises exactly that snapshot:
- It refuses with `NothingToSummariseError` (HTTP 409) when there are no results, or when the client's `result_product_ids` are no longer the latest. An unknown session raises `SessionNotFoundError` (HTTP 404).
- A per-session lock serialises requests. A model-written summary is cached by the snapshot key (search text, product ids, filters), so double clicks and repeated requests call the model once. A fallback summary is not cached, so the user can retry.
- It runs under the same per-message model-time budget as `/chat`.

---

### 5.2 Users and personal memory (removed 2026-10-21)

User profiles, personal re-ranking, preference suggestions and the `PERSONAL` intent were removed. `AgentController.handle_message(message, session_id, product_ids)` takes no user identifier, and sessions are anonymous and in memory only.

### 5.3 Presentation data for the UI (Phase 9)

- **Explicit selection.** `handle_message(..., product_ids=...)` validates the selection *before* any state change: 1–4 IDs, no duplicates, an existing session, every ID shown in it. Otherwise it raises `InvalidProductSelectionError` → 422. One ID routes to `PRODUCT_QUESTION` on that product; 2–4 route to `COMPARE`. Smalltalk and out-of-scope messages are never overridden.
- **Generation outcome.** Every answer path calls `LLMService.generate_answer_with_mode()`, which reports `llm`, `fallback` (with the reason) or `none` from the actual outcome, including a fallback after a model error mid-request. `generate_answer()` is unchanged and delegates to it.
- **Cards** (`src/agent/presentation.py`). `order_result_ids()` puts recommended products first and ignores recommendations outside the candidates. `ProductPresenter` then reads every displayed fact from SQLite by ID. LLM titles, prices and so on are never used.
- **Evidence.** `HandlerResult.evidence_pack` is the very object passed to `EvidenceValidator.validate_answer()`, and `evidence_snippets()` copies its items into the response unchanged.
- **Preference suggestions** (`src/agent/preference_suggestions.py`). Deterministic detection of first-person statements, checked against catalogue brands and categories and the user's stored preferences. Suggestions are returned only; the agent still has no write path to persistent memory.

### 5.4 Hard constraints (Phase 9 relevance fix)

- Both parsers end with `apply_deterministic_constraints()`. It reads the **stated price currency** and **product types** from the user's own words, plus the LLM's `category` when it maps to a known type, so neither path can drop or invent them. Price and rating phrases are removed from `search_text`.
- `IntentRouter._apply_constraints()` → `ConstraintResolver` (`src/agent/constraints.py`):
  - maps the request onto real categories and brands;
  - splits exclusions into brands and terms;
  - decides the price: applied in the stated currency, applied in the catalogue's single currency if none was stated (and labelled so), or **not applied** if the currency isn't used by the catalogue or is ambiguous. In that last case the turn returns a clarification with no retrieval or generation.
- Session merging on refinement keeps product types, unions exclusions, and moves the currency with the price.
- Details: [rag-design.md §6](rag-design.md).

## 6. End-to-End Evidence Preservation

For all answer-generating intents (`PRODUCT_SEARCH`, `REFINE_SEARCH`, `PRODUCT_QUESTION`, `COMPARE`), the response MUST pass through `EvidenceValidator.validate_answer()`.

The resulting `AgentResponse` includes:
- `answer`: Validated and sanitized recommendations
- `support_report`: Full audit breakdown (supported claims %, fabricated citation count, numeric check audit)
- `active_filters`: Active metadata constraints
- `shown_product_ids`: Session product history
- `parsing_confidence`: Query parser confidence

---

## 7. Known Limitations & Future Work

1. **In-Memory Ephemeral Sessions**: Sessions do not survive application restarts. Persistent session storage (e.g. SQLite / Redis) can be added in later phases.
2. **Coreference Resolution**: Positional and single-target references are supported (Section 5.1). Comparative questions about two products inside `PRODUCT_QUESTION` ("is the first one lighter than the second?") resolve only the first reference; use `COMPARE` for those.
3. **Calibrated Confidence**: Confidence scores are generated by the LLM self-evaluation or rule heuristics rather than a calibrated Bayesian predictor.
4. **Exclusions and preferences are search signals only**: they are extracted but not enforced as hard filters. `MetadataFilter` has no exclusion field, and checking "has no RGB" would itself be an unverified product claim. Offline, exclusion terms are removed from the retrieval text.
5. **Relative refinements**: "cheaper" / "more expensive" are classified as `REFINE_SEARCH` but do not compute a new price bound, because the session does not store result prices. The user must state a number ("under $50").
6. **Offline category/brand**: The rule-based parser does not extract `category` or `brand` filters (Section 3). In `rules_first` mode a brand named without a cue ("Sony headphones") is therefore a search term, not a hard filter. Keyword retrieval still ranks Sony titles first, but other brands are not excluded.
7. **Out-of-scope heuristics**: The offline out-of-scope list is a narrow pattern set. Unusual off-topic requests fall through to `PRODUCT_SEARCH`, where any answer is still limited to retrieved catalog products and passes through `EvidenceValidator` (and an empty result yields the Phase 5 "insufficient evidence" answer).
