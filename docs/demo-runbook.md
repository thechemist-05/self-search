# Self Search Demo Runbook

How to run and present Self Search, an evidence-grounded conversational product discovery assistant, for a live academic demonstration. Everything runs locally: no API keys and no cloud services. Product photos are the only part that needs internet (see below).

## 1. Requirements

| | |
|---|---|
| OS | Windows 10/11 with PowerShell |
| Python | the project virtual environment: `venv\Scripts\python.exe` (Python 3.14) |
| Local model | [Ollama](https://ollama.com) running on `http://localhost:11434` with **`llama3.2:3b`** (`ollama pull llama3.2:3b`). `llama3.2:1b` is also installed but is not used (see `evaluation-results.md`). |
| Catalogue (default) | `data\experiments\fashion-v4-20261023T014937Z\shelfsense.db`: the expanded catalogue, **86,588 products and 531,796 reviews**. It is the 50,000 v3 Fashion products plus 36,588 from Amazon Reviews 2023 *Clothing_Shoes_and_Jewelry*, *All_Beauty*, *Handmade_Products* and *Sports_and_Outdoors*: sarees, lehengas, earrings, jewellery, men's fashion, footwear, bags, beauty, home and sports. No Electronics. |
| Indexes | `data\experiments\fashion-v4-20261023T014937Z\vector_store\`: `products.index` (86,588 vectors) and `reviews.index` (531,796 vectors), 64-d offline feature-hashing embedder, `manifest.json` (consistent) |
| Product photos | `data\experiments\fashion-v4-20261023T014937Z\product_images.json`: validated photo URLs (97.3% of products), with a second photo as fallback where available |
| Previous catalogues | `fashion-v3-20261022T085353Z` (50,000 products) and `fashion-v2-20261021T094306Z` (20,000), both kept unchanged; start one with `-Catalogue <name>` |
| Ports | 8000 (API) and 8502 (UI) must be free |

The small 20-product development database (`data\processed\shelfsense.db`) is **not** used by the demo and is not changed by it.

Check Ollama before starting:
```powershell
curl.exe http://localhost:11434/api/tags      # must list llama3.2:3b
```

## 2. Start

```powershell
cd C:\Users\KIIT0001\.gemini\antigravity-ide\scratch\shelfsense
powershell -ExecutionPolicy Bypass -File scripts\run_demo.ps1
```
`scripts\run_demo.ps1`:
- verifies that the index manifest matches the database, and refuses to start if it does not;
- sets the configuration below;
- starts the API (port 8000) and the UI (port 8502) in the background, writing logs to `%TEMP%\shelfsense-demo\`.

Open **http://127.0.0.1:8502**.

| Variable set by the script | Value |
|---|---|
| `SHELFSENSE_DB_PATH` | `data\experiments\<catalogue>\shelfsense.db` |
| `SHELFSENSE_VECTOR_STORE_DIR` | `data\experiments\<catalogue>\vector_store` (the same catalogue as the database) |
| `SHELFSENSE_ESTIMATED_PRICES_ENABLED` | `true`: synthetic INR estimates for ₹ budgets. `-NoEstimates` turns them off. |
| `SHELFSENSE_LOAD_REMOTE_IMAGES` | `true`: product photos, fetched from Amazon's image host by the UI server. This needs internet; offline, cards show a neutral placeholder. `-NoImages` turns them off. |
| `SHELFSENSE_API_URL` | `http://127.0.0.1:8000` (for the UI) |
| `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE` | `1` |

**Defaults that need no setting:**
- `SHELFSENSE_OLLAMA_MODEL=llama3.2:3b`;
- `SHELFSENSE_OLLAMA_TIMEOUT=110`: model time per message, below the UI's 120 s wait;
- `SHELFSENSE_QUERY_PARSER_MODE=rules_first`: no model call is needed to understand a query, brands included.

No API key is configured by default. If you set `API_KEY` for the API, give the UI the same value as `SHELFSENSE_API_KEY`. Never paste keys into slides or logs.

**Health check:**
```powershell
curl.exe http://127.0.0.1:8000/health
# {"status":"ok","database":"connected","ollama":"available","active_sessions":0}
```
The page shows a warning only when something is wrong: "Self Search is not reachable" or "The AI model is offline …".

## 3. Timing to expect

The model runs on the CPU, so only the steps that use it are slow:
- **Searches are product-first:** the cards appear in about **2–10 s**, and no model is called. The median was 4.1–5.1 s over two runs of 24 live queries on the 86,588-product catalogue (2026-10-23); the first search after a restart is the slowest.
- **Summarise reviews (optional):** about **40–80 s** (median 59 s). It runs in the background, so the cards stay usable meanwhile.
- **Model answers:** a comparison or review question takes about 20–45 s.
- **Instant replies:** no-match searches, out-of-scope questions and greetings return in a few seconds.

If the model cannot finish within the 110 s budget, the answer is the **evidence-based fallback**, shown with a "Quick answer: …" notice; the products and reviews are still real. A failed summary shows **Try again**, and the results stay. Click **Summarise reviews** once before presenting, to warm the model up.

## 4. Demo script

Checked against the 86,588-product catalogue on 2026-10-23: product ids, titles, prices and ratings are database values. The model's wording differs between runs, and the products it recommends are listed first, so card order can vary.

| # | Type or click | What happens (from the real data) |
|---|---|---|
| 1 | **red silk saree under ₹3,000** (example button) | **New category, type + colour + material + INR budget.** **1 exact match**: *SIRIL Women's Foil Print … Poly Silk Saree* (₹2,492, "Converted from source price"). Without the budget, **red silk saree** has 32 matches, for example *Shonaya … Banarasi Silk Saree … (Blue & Red)* (id 78477); most sarees show "Price unavailable", because the source has no price for them. |
| 1a | Click **Summarise reviews** | **Optional AI summary** of the top 3 results, with "Summarising reviews of the top matches…" while it runs. Tick a Compare box meanwhile to show the page stays usable. After about a minute the "AI summary", "Top pick" badges and review-backed claims appear; the cards keep their order. |
| 2 | **bridal jhumkas under ₹1,500** (example button) | **Type + occasion + INR budget.** Chips: `Earrings`, `Bridal`, `Under ₹1,500 ✕`. **92 exact matches**, all with a converted price ≤ ₹1,500, for example *Bindhani Bollywood Style Bridal … Jhumka* (id 59135, ₹1,140). No necklaces or jewellery sets. |
| 3 | **lehnga choli** | **Synonym spelling.** Matches all 210 lehengas in the catalogue, for example *FOCIL Fully Stiched Lehnga Choli* (id 76320) and *SKY VIEW FASHION … bridal lengha choli* (id 73613, ₹5,815). |
| 4 | **running shoes under ₹3000** (example button) | **Type + INR budget.** **231 exact matches**: converted source prices such as *FUJEAK Men Running Shoes* (id 56325, ₹2,782) and labelled estimates (≈ ₹…). No football boots, sandals or shoe cleaners. |
| 5 | **women's handbag 4+ stars** | **Type + rating.** 3,474 matches, showing all three price states: *MKF Handbags ~ Designer Handbag* (id 4225, ≈ ₹1,480 estimate), *Groupcow New Ladies Evening Handbags* (id 53114, ₹996 converted from source price) and cards with "Price unavailable". |
| 6 | **men's leather belt** (example button) | **Type + material.** 638 belts whose listing states (genuine) leather, for example *Timberland PRO Men's 40mm Workwear Leather Belt* (id 52106, ₹2,326). Faux or PU leather is never counted as leather. |
| 7 | **nike running shoes** | **Brand**, detected from the catalogue with no model call. 29 exact matches, for example *Nike Men's Free Rn Running Shoes* (id 52923, ₹6,142). |
| 8 | After search 1: tick **Compare** on two cards, then **Compare selected** (or type **compare the first two**) | A comparison table of the two products, built from database values. There is also a short model comparison citing reviews of those two products only. |
| 9 | After search 1: **Ask about this** on a card, then type **is the fabric good?** | A review question about that one product. The evidence comes only from its own reviews; if they don't say, the answer reports insufficient evidence. |
| 10 | **boots under ₹100** | **Truthful empty result:** "No exact matches". **Broaden the search** offers *Any price*, and runs it as a separate new search; alternatives are never mixed into exact results. |
| 11 | **handbag under $30** | **Rupees only:** the assistant asks for the budget in ₹ and does not convert it. |
| 12 | **what is the weather in Delhi today?** | **Out of scope:** a short refusal, with no products and no model call. |

## 5. Showing evidence and prices

- **Each card** shows the product photo, name, price, rating and review count, one or two details, and up to two AI claims marked ✅ *Backed by reviews* or ⚠️ *Not verified*.
- **Reviews and details** on each card lists every claim with its full status, the cited review snippets [R1], [R2] … word for word, the price explanation, why the product matched (for example "Colour: black (stated in the title)"), and **Show all reviews**.
- **Database values only:** the model never sets a price, rating or product fact.
- **Every price is in rupees.** The Amazon US dataset's own prices are in US dollars, so they are converted at the documented rate in `config\exchange_rates.toml` (₹83.08 per US dollar, ECB reference rate, 2023-09-29). They are shown as, for example, `₹2,492` with the chip *"Converted from source price"*, and the card details name the rate and its date. Products with neither a source price nor an estimate show "Price unavailable".
- **Synthetic INR estimates:**
  - always shown as `≈ ₹…` next to the chip *"Estimated price · Not a retailer price"*;
  - used only for products **without** a source price, and only for ₹ budgets;
  - never sent to the model and never used as evidence.
- **Budgets** are in rupees only; a budget in another currency gets a request to restate it in ₹.
- **Filter chips** under the search box show exactly what was enforced. ✕ removes a price or rating filter, and **New search** (top right) starts over.

## 6. Restart safely

Streamlit keeps imported code in memory, so **always restart both services after a code change**. A stale UI can crash.
```powershell
# stop only the demo services (ports 8000 and 8502)
foreach ($port in 8000, 8502) {
  $c = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
  if ($c) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)"
    Stop-Process -Id $c.OwningProcess, $p.ParentProcessId -Force -ErrorAction SilentlyContinue
  }
}
powershell -ExecutionPolicy Bypass -File scripts\run_demo.ps1
# to fall back to the previous 50,000-product catalogue:
# powershell -ExecutionPolicy Bypass -File scripts\run_demo.ps1 -Catalogue fashion-v3-20261022T085353Z
```
Refreshing the browser starts a new conversation, and restarting the API starts fresh sessions. No data is written to the catalogue.

**Optional live self-test (about 15 minutes, real model calls; 19 checks):**
```powershell
.\venv\Scripts\python.exe scripts\e2e_demo_check.py --api http://127.0.0.1:8000 --db data\experiments\fashion-v4-20261023T014937Z\shelfsense.db
```

## 7. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `run_demo.ps1`: "Port 8000 is already in use" | Another API is running. Stop it (section 6), then start again. |
| `run_demo.ps1`: "Index manifest does not match the database" | The database and indexes differ. Do not start; see `deployment.md`. |
| Warning: "The AI model is offline …" | Ollama is not running. Start the Ollama app and check `curl.exe http://localhost:11434/api/tags`. |
| "The answer took too long, so the request was stopped" | The model is busy or slow. Wait, then retry, and avoid several searches at once. |
| "Self Search is not reachable" | The API is not running. Check `%TEMP%\shelfsense-demo\api.log` and restart (section 6). |
| Product cards show "Image unavailable" | No internet, or the product has no valid photo in the source (2.7% of products); search still works. |
| UI shows a `TypeError` or old controls | A stale Streamlit process. Restart both services (section 6). |

## 8. Known limitations (not blockers)

- **Speed:** CPU-only generation takes about 1–2 minutes per AI summary. A GPU would make it interactive.
- **Strict means fewer results:** a product counts only if its listing *states* the type, colour or material. Many listings don't state a colour, so colour searches can show fewer cards than the catalogue might hold; the page says how many were left out.
- **Price coverage:** 34.1% of products (29,502) have a source price (USD, shown converted to ₹), 10.0% (8,622) have an illustrative INR estimate (footwear and bags), and 56.0% (48,464) have no price, so they never meet a budget. Most sarees and lehengas have no source price.
- **Fewer sarees and lehengas than planned:** the source holds only 1,381 qualifying sarees and 210 lehengas, against allocations of 8,000 and 7,000 (`data-sources.md`).
- **Lexical matching:** types and colours come from listing text; a misleading title can still slip through. For example, *red summer dress under ₹2000* returns a men's kurta set whose title says "Red … Party Dress Suit" (measured precision is in `evaluation-results.md`).
- **Claim support:** reviews are short and informal, so many claims are marked *Not verified*. This is the validator working as designed.
- **Data licence and images:** Amazon Reviews 2023 states no licence, so this catalogue is for local academic use only. Photos are shown from Amazon's image host and are not stored (`data-sources.md`).
