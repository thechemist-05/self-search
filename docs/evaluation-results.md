# Evaluation Results

*To be filled in Phase 10 with tables, charts, and analysis comparing retrieval variants A–D.*

## Local answer model: llama3.2:3b vs llama3.2:1b (2026-10-21)

**Command:**
```
python scripts/benchmark_llm.py --models llama3.2:1b llama3.2:3b \
    [--db <database> --index-dir <indexes>] --out eval/results/<file>.json
```

**Raw results:** `eval/results/llm_benchmark_production.json` and `eval/results/llm_benchmark_fashion.json`.

**Setup:**
- **Queries:** all 20 in `eval/queries.jsonl`. Each runs in a fresh session through `AgentController`, exactly as `/chat` does.
- **Pipeline, same for both models:** rules-first query parsing (no model call for any of these queries), hybrid retrieval, evidence validation including numeric checks, and the safe fallback.
- **Settings:** Ollama timeout 180 s (unchanged), CPU-only (no GPU offload), one model loaded at a time after a warm-up call.
- **Data:** every run used a temporary copy of its database, so neither source database was modified.

**Metrics:**
- *Latency* is the end-to-end `handle_message` time.
- *LLM success* means a schema-valid model answer was used (`generation.mode = "llm"`).
- *Support rate* is the share of an answer's claims that `EvidenceValidator` marked supported, averaged over model answers.

### Production catalogue (`data/processed/shelfsense.db`; 20 products, the catalogue these queries were written for)

| | llama3.2:1b | llama3.2:3b |
|---|---|---|
| Median / mean / max latency | 20.6 / 20.4 / 23.8 s | 36.9 / 37.8 / 54.3 s |
| LLM success / fallback rate | 20/20 (100%) / 0% | 20/20 (100%) / 0% |
| Valid JSON on first attempt | 100% | 100% |
| Answers with every claim supported | 6/20 (30%) | 15/20 (75%) |
| Mean support rate | 0.45 | 0.81 |
| Unsupported claims | 22 of 40 | 8 of 44 |
| Fabricated citations / cross-product / numeric mismatches | 0 / 0 / 0 | 0 / 0 / 0 |
| Relevant product retrieved | 20/20 | 20/20 |

### Fashion experimental catalogue (20,000 products; latency stress test)

The eval queries are electronics and outdoor queries, so here they mostly retrieve loosely related fashion items: 6 of 20 retrieve nothing, and the labelled relevant IDs do not exist. **Read this table for latency and robustness, not for answer quality.** The evidence pack is much larger than in production.

| | llama3.2:1b | llama3.2:3b |
|---|---|---|
| Queries with products | 14 | 14 |
| Median / mean / max latency (answered) | 86.3 / 116.2 / 306.3 s | 179.9 / 150.8 / 186.9 s |
| LLM success / fallback rate | 10/14 (71%) / 29% | 8/14 (57%) / 43% |
| Fallback reasons | 3 timeouts, 1 invalid JSON after retries | 6 timeouts |
| Mean support rate (model answers) | 0.05 | 0.25 |
| Cross-product mismatches (caught by the validator) | 5 | 4 |
| Fabricated citations / numeric mismatches | 0 / 0 | 0 / 0 |

### Conclusion
- **Keep `llama3.2:3b` as the default.** `llama3.2:1b` is about 1.8× faster on the production catalogue, but its answers had nearly three times as many unsupported claims (22 vs 8) and a support rate of 0.45 vs 0.81. On the Fashion catalogue it also produced the only invalid-JSON fallback.
- **Speed should come from smaller prompts, not a weaker model.** Neither model fabricated citations or numbers, because the validator caught every mismatch.
- **On the Fashion catalogue, `llama3.2:3b` answer generation on CPU sits at the 180 s timeout** (43% timeouts), even with no model call for query parsing. Making that catalogue interactive needs a smaller answer prompt (fewer products or evidence items) or a GPU. Raising timeouts would only hide the problem.

## Final catalogue latency: before and after optimisation (2026-10-22)

**Setup:**
- **Catalogue:** Amazon Reviews 2023 Fashion, 20,000 products and 126,977 reviews, with synthetic INR estimates enabled.
- **Model:** `llama3.2:3b` on CPU, with no GPU.
- **Command:** `scripts/benchmark_llm.py` through `AgentController`, as `/chat` runs it, on a temporary database copy.
- **Queries:**
  - `eval/fashion_queries.jsonl`: 12 representative Fashion searches, plus one comparison and one review question as follow-ups (14 turns);
  - `eval/queries.jsonl`: the 20 existing electronics and outdoor queries. These are mostly off-catalogue here, and their relevance labels refer to production IDs.

**Raw results:**
- `eval/results/latency_final_fashion_before.json` (before);
- `eval/results/latency_final_fashion_prompt_only.json` (first step only: prompt budget, 180 s budget);
- `eval/results/latency_final_fashion_after.json` and `latency_final_evalset_after.json` (final);
- `eval/results/llm_benchmark_fashion.json` (earlier eval-set run).

| Fashion queries (14 turns) | Before | After |
|---|---|---|
| Answered by the model (`mode=llm`) | 2/14 (14%) | 13/14 (93%) |
| Searches answered by the model | 0/12 | 11/12 |
| Median / mean / max latency | 183.6 / 164.2 / 185.9 s | 98.6 / 90.7 / 112.5 s |
| Turns over the UI's 120 s timeout | 12/14 | 0/14 |
| Answer prompt size (search) | ~10,400–12,200 chars | ~4,500–6,500 chars |
| Compare / review question | 52.6 s / 38.9 s | 59.0 s / 36.4 s |
| Mean claim support rate (model answers) | 0.50 (2 answers) | 0.64 (13 answers) |
| Fabricated citations | 0 | 0 |
| Numeric / cross-product mismatches caught by the validator | 0 / 0 | 3 / 4 |

| Existing eval set on the Fashion catalogue (20 queries) | Before (2026-10-21) | After |
|---|---|---|
| Model answers among queries with products | 8/14 (57%) | 10/14 (71%) |
| Median / max latency | 179.9 / 186.9 s | 84.1 / 114.6 s |
| Mean support rate | 0.25 | 0.45 |

**What changed:**
1. Rules-first query understanding, so ordinary queries need no model call.
2. The model writes about the top 4 retrieved products only. Evidence is query-ranked and deduplicated: at most 2 reviews each, about 60 words per snippet, 800 tokens in total.
3. A shorter answer: at most 3 recommendations and 2+1 claims each, with no repeated titles.
4. One 110 s model-time budget per message, below the UI's 120 s, which also covers JSON retries.
5. `num_predict` and `keep_alive` are set on Ollama requests.
6. Retrieval ranks products containing all of the user's words first. "Find running shoes" used to return knee-high boots and slippers first.

**Still true:**
- On this CPU, a model answer on this catalogue takes about 65–110 s. That is about 25 tokens/s to read the prompt and about 9 tokens/s to write the answer.
- When the budget runs out, the labelled, evidence-based fallback is shown instead.
- Reaching the spec's interactive target needs a GPU, or a smaller model; `llama3.2:1b` was measured and rejected on answer quality.

## 50,000-product catalogue latency (2026-10-22)

**Setup:** the same 14 Fashion turns (`eval/fashion_queries.jsonl`) and command as above, now on `data/experiments/fashion-v3-20261022T085353Z` (50,000 products, 271,939 reviews) with strict matching, `llama3.2:3b` on CPU and INR estimates enabled. **Raw results:** `eval/results/latency_fashion50k.json`.

| Fashion queries (14 turns) | 20k, after (above) | 50k, strict matching |
|---|---|---|
| Answered by the model (`mode=llm`) | 13/14 | **14/14** (no fallback) |
| Searches: median / min / max | — | 77.5 / 25.9 / 112.5 s |
| All turns: median / mean / max | 98.6 / 90.7 / 112.5 s | 62.8 / 67.9 / 112.5 s |
| Turns over the UI's 120 s timeout | 0/14 | 0/14 |
| Compare / review question | 59.0 s / 36.4 s | 52.1 s / 38.8 s |
| First-attempt valid JSON | — | 14/14 |
| Mean claim support rate (model answers) | 0.64 | 0.55 (51 claims, 21 not supported) |
| Fabricated citations | 0 | 0 |
| Numeric / cross-product mismatches caught by the validator | 3 / 4 | 0 / 6 |

**Reading:**
- The larger catalogue did not make answers slower: retrieval with strict filters takes about 1–4 s, and the time is dominated by the model.
- The slowest search (112.5 s wall time) includes retrieval and parsing outside the 110 s model budget. It is still inside the UI's 120 s.
- The lower support rate comes from one run of 14 turns: the model's wording varies between runs, and the validator is unchanged. Unsupported and cross-product claims are flagged in the answer, never hidden.

## Product-first search and optional AI summary (2026-10-22)

**Change:** searches return their products as soon as retrieval finishes, with no model call. The AI summary of a result set is optional (`POST /chat/summary`) and covers the top 3 results instead of 4. Questions and comparisons are unchanged.

**Setup:** the same 12 Fashion queries plus 2 follow-ups (`eval/fashion_queries.jsonl`), on the 50,000-product catalogue, with `llama3.2:3b` on CPU. Each search is timed on its own. Its optional summary and the follow-ups are timed separately (`scripts/benchmark_llm.py`, which now reports Ollama's own prompt-token counts).

| | Before (every search called the model) | Product-first + optional summary |
|---|---|---|
| Time until products are returned, median / max | 77.5 / 112.5 s (the whole answer) | **1.36 / 3.1 s** |
| Searches calling the model | 12 / 12 | **0 / 12** |
| Optional summary, median / max | — | 58.8 / 79.5 s |
| Compare / review question | 52.1 s / 38.8 s | 22.5 s / 19.8 s |
| Answered by the model (no fallback) | 14 / 14 | 14 / 14 |
| Fabricated citations / numeric mismatches | 0 / 0 | 0 / 0 |
| Mean claim support | 0.55 | 0.44 |
| Cross-product citations caught by the validator | 6 (of 51 claims) | 6 (of 54 claims) |

- **Raw results:** before: `eval/results/latency_fashion50k.json`; after: `eval/results/latency_fashion50k_product_first_oldprompt.json` (this is the prompt that ships; see below).
- **Lower claim support:** it comes from summarising 3 products instead of 4 (fewer cited reviews to choose from) and from run-to-run variance. Unsupported claims are flagged, never hidden, and the validator is unchanged.
- **Follow-up timings:** they vary a lot between runs (36–44 s in the other two runs below), so the faster figures here are not claimed as an improvement.

### Prompt trimming: measured and rejected

Three prompt texts were run on the identical product-first workload. One run each; the model's output varies between runs.

| Variant | Summary median / max | Prompt tokens (median) | Mean support | Cross-product citations | Answers with one |
|---|---|---|---|---|---|
| **B: original text (shipped)** | 58.8 / 79.5 s | 1,252 | 0.437 | **6 / 54 claims** | **6 / 14** |
| A: trimmed (shorter rules, no ASIN, no placeholders, short reminder) | 51.8 / 62.9 s | 1,068 | 0.44 | 11 / 45 claims | 10 / 14 |
| C: trimmed, but ASINs kept in the evidence lines | 52.3 / 68.3 s | 1,136 | 0.487 | 10 / 48 claims | 10 / 14 |

- **What the trim did:** it saved about 15% of prompt tokens and about 6–7 s per summary.
- **The cost:** citations of another product's review roughly doubled (11% → 21–24% of claims) and spread across most answers. Restoring the evidence ASINs (C) did not fix this.
- **Decision:** grounding comes first, so the original prompt text is kept. Raw results: `latency_fashion50k_product_first.json` (A), `latency_fashion50k_product_first_oldprompt.json` (B), `latency_fashion50k_product_first_trimmed_evidence_asin.json` (C).
- **Prefix reuse:** the fixed instructions already come first. Ollama's prompt-token counts are about the full prompt length on every call (B: 843–1,341 tokens), so this measurement shows **no evidence** of reuse between calls, and no speed-up is claimed from it.

### Live checks (services restarted with this code)

- **API:** `scripts/e2e_demo_check.py`, 14/14 passed (`eval/results/e2e_demo_check_product_first.json`).
  - Searches took 0.5–2.3 s with `mode: none` (the model was not called).
  - The optional summary took 61.7 s (3 of 3 claims supported). A repeated request was served from the cache in 0.01 s.
  - The review question took 42.4 s and the comparison 41.2 s.

## Strict matching: returned-result precision (2026-10-22)

**Command:** `scripts/evaluate_strict_matching.py`.

**Setup:**
- **Pipeline:** the real controller (rules-first parsing, constraint resolution, hybrid retrieval with each catalogue's own FAISS indexes), with the model unavailable. Only retrieval is measured, on a temporary copy of each database.
- **Judging:** every returned product is judged against hand-written per-query criteria (title regexes, colour, INR budget, brand) in the query file. These are independent of the rules in `src/retrieval/constraints.py`.
- **Relaxed baseline:** the same code with the strict rules switched off. A type matches any synonym anywhere in the title or description, and colour, material and brand only affect ranking. This approximates the earlier lexical behaviour.
- **Query sets:**
  - `eval/strict_queries.jsonl` (20 queries) was used **during development**: its errors led to three fixes (the "short" adjective, "buckle for backpack", and a feeding-pump bag returned for "green heels"), so its strict scores are optimistic.
  - `eval/strict_queries_holdout.jsonl` (12 queries) was written afterwards and **not** used to change the rules.

**Raw results:** `eval/results/strict_matching_eval.json` and `strict_matching_eval_holdout.json`.

| Development set (20 queries) | 20k relaxed | 20k strict | 50k relaxed | 50k strict |
|---|---|---|---|---|
| Precision of returned results (micro) | 0.625 | **1.000** | 0.745 | **0.994** |
| Results returned | 200 | 153 | 200 | 180 |
| Queries with ≥ 1 relevant result | 16/20 | 16/20 | 18/20 | 18/20 |
| Zero-result queries | 0 | 4 | 0 | 2 |

| Held-out set (12 queries) | 20k relaxed | 20k strict | 50k relaxed | 50k strict |
|---|---|---|---|---|
| Precision of returned results (micro) | 0.792 | **1.000** | 0.867 | **1.000** |
| Results returned | 120 | 111 | 120 | 120 |
| Queries with ≥ 1 relevant result | 12/12 | 12/12 | 12/12 | 12/12 |

**Reading the results:**
- **The relaxed baseline always fills 10 cards,** including white or heeled boots for "black heels", wallets for "black handbag", and dress shoes for "red dress".
- **Strict mode returns fewer cards, and on these sets nearly all are correct.**
  - The one strict miss on 50k was a "Tactical Sling Bag … Backpack" that the judge rejected only because its title says "One Strap".
  - Zero results are truthful. "red summer dress under ₹2000" has no INR-priced dress in either catalogue (apparel has no estimates). "green heels": neither catalogue has a heel stated as green. Before the final fix, strict mode returned a "Kangaroo Joey Bag For Feeding Pumps" here; the bag veto removed it, and the judge was tightened at the same time. On 20k, "men's leather belt" and "pink swimsuit" had no matching products; the 50k catalogue covers them.
- **Retrieval time** with strict filters on 50,000 products is about 0.7–4 s per query after optimisation, versus about 1.5 s relaxed. The final run's medians (about 5–7 s) are higher because it ran alongside the full test suite. The full answer is dominated by the model (about 1–2 min).
- **Not claimed:** 32 queries are not a full relevance study. Precision measures whether returned items meet the stated requirements, not taste or ranking quality.

## Expanded 86,588-product catalogue (2026-10-23)

**Setup:**
- **Command and pipeline:** `scripts/evaluate_strict_matching.py`, the same pipeline and relaxed baseline as above, on `data/experiments/fashion-v4-20261023T014937Z` with its own indexes.
- **New query set:** `eval/strict_queries_v4.jsonl` (20 queries on sarees, lehengas and earrings), written before the v4 data was built or inspected.

**Results:**

| Query set | Relaxed (micro) | Strict (micro) | Strict results | Zero-result queries | Strict median s |
|---|---:|---:|---:|---:|---:|
| Development set (20), v3 | 0.745 | 0.994 | 180 | 2 | 5.6 |
| Development set (20), v4 | 0.625 | **1.000** | 184 | 0 | 5.6 |
| Held-out set (12), v4, before the leather fix | 0.800 | 0.975 | 120 | 0 | 10.1 |
| Held-out set (12), v4, after the leather fix | 0.800 | **0.983** | 120 | 0 | 10.0 |
| New categories (20), as pre-registered | 0.605 | 0.740 | 181 | 1 | 5.7 |
| New categories (20), revision 1 of the judge | 0.800 | **0.961** | 181 | 1 | 5.1 |

**Raw results** (in `eval/results/`):

| Set | File |
|---|---|
| Development | `strict_matching_v3_vs_v4.json` and `strict_matching_v4_dev.json` |
| Held-out | `strict_matching_v4_holdout.json` |
| New categories | `strict_matching_v4_new_categories.json` and `strict_matching_v4_new_categories_rev1.json` |
| Live API | `live_api_v4.json` |
| End-to-end checks | `e2e_demo_check_v4.json` |

**Reading the results:**
- **No regression on the existing sets.** On v4, "green heels" and "red summer dress under ₹2000" now return matches. The dress match is a keyword-stuffed men's kurta set ("… Red … Party Dress Suit"), which the title-based judge also accepts.
- **The pre-registered judge for the new categories was wrong about sarees.** It rejected every title containing "blouse", but sarees are sold "with unstitched blouse piece".
  - Revision 1 (`eval/strict_queries_v4_rev1.jsonl`, made after seeing the results and labelled as such) rejects only blouse products. No other criterion changed. Both results are reported.
- **Remaining strict misses on the new categories (7 of 181):**
  - 4 art-silk sarees for "silk saree". The system treats art silk as silk; the judge does not.
  - 2 multi-pair earring packs for "earrings".
  - 1 earring "set" for "gold plated earrings under ₹1,000".
- **"velvet lehenga" returns nothing, correctly.** In the three lehengas that mention velvet, only the blouse is velvet.
- **Leather fix (made during this evaluation):** a listing that states both "leather" and PU/faux leather (for example "Coffee Leather Wallet … PU Leather Wallet") is no longer counted as genuine leather, unless it also says genuine, real, full-grain, top-grain or cowhide.
  - This moved 38 v4 products (13 in v3) to faux leather; each one states PU or faux leather.
  - The two remaining held-out misses are a "Coated Leather" wallet (78% PU) and a wallet whose title omits leather but whose details state "Genuine Leather". The judge only reads titles.
- **End-to-end:** `scripts/e2e_demo_check.py` passed 19/19 on v4 after three outdated checks were corrected:
  - check 8 now accepts converted INR prices;
  - check 16 now allows sarees sold "with blouse";
  - check 20 now uses the v4 saree type keys.
- **Retrieval time:** strict medians were 5–10 s per query in these runs. Live searches through the API had a median of 4.1–5.1 s (24 queries).
