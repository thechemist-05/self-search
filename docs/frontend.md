# Frontend (Phase 9)

Streamlit chat UI in `frontend/`. It talks to the Self Search API **only** through its JSON contract (see [api-documentation.md](api-documentation.md)) and never imports backend modules.

## Running

```
# Terminal 1: API (loopback only)
.\venv\Scripts\python.exe -m uvicorn src.api.app:app

# Terminal 2: UI (loopback only, via .streamlit/config.toml)
.\venv\Scripts\python.exe -m streamlit run frontend/app.py
```

Open http://127.0.0.1:8501.

| Variable | Default | Purpose |
|---|---|---|
| `SHELFSENSE_API_URL` | `http://127.0.0.1:8000` | API base URL (`http://api:8000` under Docker compose) |
| `SHELFSENSE_API_KEY` (or `API_KEY`) | unset | Sent as `X-API-Key` **by the Streamlit server process only**; never rendered into the page or sent to the browser |
| `SHELFSENSE_LOAD_REMOTE_IMAGES` | off | Remote product images load only when this is exactly `true`/`1`/`yes`/`on` |

`.streamlit/config.toml` binds to `127.0.0.1` and sets `browser.gatherUsageStats = false`. Streamlit's usage telemetry is on by default and would otherwise send data off the machine.

## Structure

| File | Role |
|---|---|
| `frontend/app.py` | Page layout: compact header, the single search box, filter-chip row, ask-about bar, latest answer, earlier answers (toggle), compare button, footer disclaimer |
| `frontend/state.py` | Session state and **every** API action. Widgets queue a request (`queue`); `run_pending` sends it under one spinner. The optional AI summary runs in a background thread (`request_summary` / `poll_summary`). Rendering never calls write endpoints |
| `frontend/components/turn.py` | One answer: compact notices, AI summary, card grid, "Reviews and details", compare table |
| `frontend/view_models.py` | Pure display rules (unit-tested): claim status labels, banners, image decisions, labels, merging |
| `frontend/api_client.py` | HTTP client; maps errors to kinds (`unreachable`, `validation`, `conflict`, …) |
| `frontend/config.py` | Environment configuration |

## Layout and theme (redesign)

- **Theme (Stage 1 visual design):** defined once in `frontend/theme.py` (`PALETTE`, `CSS`) and mirrored in `.streamlit/config.toml`; the UI targets `streamlit==1.66.0` (pinned, because the CSS hooks into its `data-testid` and `st-key-*` classes).
  - **Colours (editorial redesign, 2026-10-22):** charcoal ink `#1B1F2B` on ivory `#F8F5EF`, with `#F1ECE3` image wells and hairline `#E4DDD2` card outlines. Restrained cobalt `#2346D1` is used for links, focus and the selected state; the primary button is ink. Coral (destructive and "not supported"), mustard (warnings), mint ("supported") and lavender are muted. Each accent has a *deep* shade for text and a *tint* for fills.
  - **Fonts:** Streamlit's bundled Source Serif 4 (display), Source Sans 3 (body) and Source Code Pro (labels and evidence IDs). No font downloads or remote font services.
  - **Buttons:** primary (filled ink), secondary (outline), quiet (Cancel, New search), pills (examples, filter chips, broaden options). Disabled buttons are dashed and muted; while a request runs, buttons dim and show a wait cursor.
  - **No decorative background or ambient animation** (removed in the redesign). The only motion is the loading placeholder pulse, which stops under `prefers-reduced-motion: reduce` along with card and button transitions.
  - **Contrast:** every text pair is ≥ 4.5:1 and every UI-border pair ≥ 3:1 (`TestThemeAccessibility`). Streamlit's default caption fade (`opacity: .6`) and its light avatar discs are overridden because they fell below 4.5:1 in the browser check.
- **Editorial layout (2026-10-22): no sidebar.** The page uses the full width:
  1. A compact **Self Search** header. Once a search exists, a **New search** button on the right clears the conversation.
  2. **Before the first search only:** a short hero ("Find exactly what you have in mind.") with one sentence explaining that results come from real reviews.
  3. **One search box** with a Search button. It is used for searches and follow-up questions; there is no second chat input. Six example queries show before the first search.
  4. **While a request runs**, feedback for that request only:
     - a search shows "Finding matching products…" and placeholder cards (it takes a few seconds; no model call);
     - a question about, or comparison of, selected products says the local AI model is reading the reviews.

     Cancel only leaves "Ask about this" mode; a running request cannot be stopped from the page.
  5. **A results head:** the query and the exact-match count ("56 exact matches" or "No exact matches"). Results are never padded. When some products were left out because their listing doesn't state the requested colour or material, a note says how many. With no exact matches, **Broaden the search** buttons (for example *Any colour*, *Any price*) each run a new, clearly separate search.
  6. **Filter chips** directly under the search box, only when filters apply: for example `Footwear`, `Under ₹3,000`, `4+ stars`, `Without leather`. Price, rating, brand and category chips have ✕ to remove them; type and exclusion chips follow each new search. The row is built from the API's constraint report, so it shows exactly what was enforced.
  7. **Problems only:** API unreachable, AI model offline, or a flash message. There is no status panel.
  8. **The latest answer**: the question, compact notices, the cards, and the comparison table. Earlier answers appear under a "Show earlier results" toggle.
     - **The AI summary is optional (2026-10-22).** A search shows its cards with no AI text. **Summarise reviews** asks `POST /chat/summary` for a summary of the top 3 results.
     - The request runs in a background thread. Only a small fragment polls it, every 2 s, showing "Summarising reviews of the top matches…". The cards stay on screen and every control keeps working.
     - **When it is ready**, the page redraws once with the "AI summary", "Top pick" badges and the claims, with the cards in the same order.
     - **If it fails**, a short warning and **Try again** appear and the results are untouched.
     - **No duplicate requests:** a running summary is never resubmitted (reruns, double clicks), and the API also caches a finished summary per result set. A summary for results no longer on screen is discarded.
  9. **One small footer disclaimer.**
- **Notices that prevent misreading stay visible**, in short form:
  - "Quick answer: the AI model did not answer in time, so this summary is built directly from the reviews shown."
  - "Only 1 of 3 claims are backed by the cited reviews (…). Unbacked claims are marked ⚠️."
  - "Not enough evidence for a reliable recommendation."
  - "Not applied: <requirement>. <reason>"
- **Cards, in priority order:**
  - product image (or the neutral placeholder);
  - a "Top pick" badge on recommended products;
  - the name, clamped to two lines;
  - the price. A synthetic INR estimate is shown as `≈ ₹2,150` **always** next to the chip `Estimated price · Not a retailer price`;
  - rating · ratings count · stored review count;
  - one or two details (brand, first feature);
  - up to two model claims, each with a worded status (✅ *Backed by reviews* / ⚠️ *Not verified*);
  - **Ask about this** and **Compare**.
  - **"Reviews and details"** holds every claim with its full status, the cited review snippets, the estimate's range explanation, the matched requirements, and "Show all reviews".

## Display rules

**Product facts.** Title, brand, category, price, ratings, features, attributes and images come only from `response.products`, which are database records. The LLM's `recommendations[].title` is ignored. Ratings are labelled as the source dataset's figures ("4.3★ catalogue rating (1,250 ratings)") and kept separate from "N reviews stored locally".

**Claims.** Each claim is matched to its `support_report.claim_results` entry by product and text (with the `[UNVERIFIED] ` prefix removed). It is shown with an icon **and words**:

| Status | Shown as |
|---|---|
| `SUPPORTED` | ✅ Supported by cited reviews |
| `UNSUPPORTED_LOW_SIMILARITY` | ⚠️ Not supported by the cited reviews |
| `FABRICATED_CITATION` | ⚠️ Cited evidence not found |
| `CROSS_PRODUCT_MISMATCH` | ⚠️ Cited review is about a different product |
| `NUMERIC_MISMATCH` | ⚠️ Number does not match the catalogue |
| `UNVERIFIABLE` | ❔ Could not be checked against catalogue data |
| no matching result | ⚠️ Unverified (never shown as verified) |

**Always visible, never collapsed:**
- **Invalid support report:** a red banner, e.g. "Only 2 of 9 claims are backed by the cited reviews (…). Unbacked claims are marked ⚠️". A missing report says every claim should be treated as unverified.
- **Insufficient evidence:** a notice that includes `missing_information`.
- **Offline fallback** (`generation.mode == "fallback"`): "Quick answer: <why>, so this summary is built directly from the reviews shown." The reason is one of: model not running, did not answer in time, reply unusable.
- **Summary:** labelled "AI summary". The validator checks claims, not the summary, and each claim carries its own status.

**Reviews and details** (formerly "Why this result?"). For each claim: its status, then each cited snippet (star rating, verified-purchase flag, review ID, quoted text) from `response.evidence`, which is the exact pack the validator checked. It also shows:
- citations missing from that pack;
- uncited evidence for the product;
- which active filters the product's database values satisfy;

**Untrusted text.** Review and AI text is markdown-escaped before display, so `$5…$10` cannot render as LaTeX and `[x](url)` cannot become a link.

**Images.** Each card shows the product's own image when remote images are enabled (`SHELFSENSE_LOAD_REMOTE_IMAGES=true`, which `scripts/run_demo.ps1` sets; pass `-NoImages` to turn it off).
- **Where the URLs come from:** for the Fashion catalogue, from `product_images.json` next to the database. `scripts/extract_product_images.py` builds that file from the verified source metadata, matching by exact `parent_asin` and using the `MAIN` image; see `data-sources.md`. The API uses an entry only if `products.image_url` is empty and the entry's `external_id` equals the product's.
- **How they are fetched:** by the UI server, not the browser: http(s) only, `image/*` only, between 1.5 KB and 2 MB, with a 5 s timeout.
- **Expanded catalogue (v4):** `product_images.json` is built by `scripts/validate_product_images.py`, which keeps up to two URLs per product that returned a real image when checked. The card tries the first URL, then the second (`image_alternates` in the API), before giving up.
- **Fallback:** a neutral SVG placeholder ("Image unavailable", alt text "Image unavailable for <title>") is used when:
  - there is no valid URL (2.7% of v4 products);
  - remote images are off;
  - the URL is on a reserved documentation domain (`example.com/.org/.net`, `.example`, `.invalid`, `.test`, `.localhost`);
  - the fetch fails, for example when offline.
- **Caching:** successful fetches are cached for 1 h and failures for 5 min. Placeholders are never presented as product photos, and search works without any images.

**Aspects.** Aspect analysis (Phase 2) is not implemented, so it is not shown. Nothing is synthesised.

## Interactions

| Control | API call | Notes |
|---|---|---|
| Search box / example | `POST /chat` | No user identifier is sent |
| "Ask about this" | next search-box `POST /chat` with `product_ids: [id]` | "Asking about …" bar with Cancel; the box placeholder changes |
| "Compare" (2–4), then "Compare selected" | `POST /chat` with `product_ids` | The button is disabled outside 2–4 |
| Filter chip ✕ | `DELETE /sessions/{id}/filters/{name}` (both bounds for a range) | Applies to the next search |
| "Show all reviews" | `GET /products/{id}` | |

## States

| State | Behaviour |
|---|---|
| Loading | One spinner under the search box while any request (search, example, compare, question) runs |
| API unreachable | Red banner; Search button disabled |
| Request takes longer than the UI's 120 s | "The answer took too long, so the request was stopped …" (never "not reachable") |
| Local model offline (`/health`) | Page-level warning, plus the per-answer fallback warning from `generation` |
| 422 / 404 / 409 / 503 / 5xx | Message in the chat. A 409 starts a new conversation; 503 explains the migration |
| Empty results | Info message suggesting the user remove a filter or rephrase |

## Accessibility and responsiveness

- Native Streamlit widgets only (keyboard-operable). Every button has a text label; icons are always paired with words.
- Validation status always has a text label and an icon, never colour alone.
- Focus: a 3 px cobalt-deep outline on every focusable control (`:focus-visible`), on the visible label of checkboxes and toggles, and an inset ring on keyboard-focusable scroll regions (main pane, tables).
- Images carry descriptive `alt` text, and headings run title → card.
- Cards sit in a wrapping grid (17 rem basis) that becomes one column on narrow screens; button rows inside cards wrap instead of clipping; wide tables scroll horizontally inside their box.

**Checked in headless Chrome** with `scripts/ui_check.py` (below) at 1440, 834, 390 and 320 px, and at a 1280 px window under 200 % zoom. **Not checked:** screen-reader output, real phones, browsers other than Chrome, and Windows high-contrast mode.

## Tests

- `tests/unit/test_frontend_view_models.py`: display rules, image decisions, escaping, merging, API client errors and image fetching.
- `tests/integration/test_frontend_app.py`: the real app script under `streamlit.testing.v1.AppTest` with a recording fake client. Covers cards, statuses, banners, states, images, compare, ask-about, filter chips, price-status labels, the absence of profile controls, labels, alt text, and the API key never being rendered.
- `tests/unit/test_frontend_view_models.py::TestThemeAccessibility`: palette and config match, contrast pairs, decorative-layer safety (opacity, pointer events, stacking), reduced motion, local fonts only, focus outlines kept.
- `scripts/ui_check.py`: browser-level checks in headless Chrome over the DevTools protocol, using packages already installed (`websockets`, `requests`). Run it against an API and UI that are already running (it only reads):

  ```bash
  python scripts/ui_check.py --url http://127.0.0.1:8602 --out ui_check_output
  ```

  At each viewport it clicks through search, compare, ask-about and cancel with real mouse events. It then checks the layout: no sidebar, no second input, the filter row between the search box and the results, and no profile controls. It also counts real product images versus placeholders. In each state it checks:
  - no horizontal overflow;
  - every control is top-most at its centre (nothing, including the decoration, covers it);
  - no clipped or overlapping controls and no truncated labels;
  - rendered text contrast;
  - visible focus on every Tab stop;
  - that reduced motion stops the animation.

  It also measures idle CPU and saves screenshots. It exits with status 1 on any problem.

## Prices: rupees only (2026-10-23)

Every displayed price and budget is in INR, with Indian digit grouping (₹799, ₹1,499, ₹12,999, ₹1,25,000). The page uses only the API's explicit fields (`price`/`currency` = normalised INR, `price_status`, `price_conversion`, `estimated_price`). It never infers a currency from a number, never swaps a symbol, and never converts anything itself.
- **Source price in INR** (`verified`): `₹1,499` with the chip "Source price". In the comparison table: "₹1,499 (source price)".
- **Converted source price** (`converted`): `₹1,968` with the chip "Converted from source price". **Reviews and details** explains the rate, for example: "The source lists this price in another currency; it is converted to rupees at ₹83.08 per US dollar (ECB reference rate, 2023-09-29)." The original amount is never shown.
- **Estimated price:** `≈ ₹2,020` with the warning chip "Estimated price · Not a retailer price". **Reviews and details** gives the illustrative range and config version. In the comparison table: "≈ ₹2,020 (estimate, not a retailer price)".
- **Unknown price:** "Price unavailable". So does any card that arrives with a non-INR currency (an old response): no dollar amount is ever displayed.
- **Budget chips** are INR only ("Under ₹3,000"). A budget in another currency is not applied; the notice reads "Not applied: Budget not in rupees."
- **Foreign amounts in text:** `strip_foreign_prices` removes any dollar, euro or pound amount from the AI summary, claims, titles, features and review quotes before display, replacing it with "[amount removed: prices are shown in ₹]". The API already removes them from AI text; this display-time guard also covers source text.
- **Budget checks** come only from the API's per-card `constraint_checks`. The page's own fallback never compares a price with a budget.
- Tests: `TestPriceStatusDisplay`, `TestImageFallback` in `tests/integration/test_frontend_app.py`; `TestCards` in `tests/unit/test_frontend_view_models.py`.

## No user profiles (2026-10-21)

The app opens directly to product search. There is no profile creation, profile ID, consent, preferences, wishlist, feedback, export or delete. Tests: `TestNoProfiles` in `tests/integration/test_frontend_app.py`.
