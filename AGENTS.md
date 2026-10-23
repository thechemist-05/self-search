# Self Search

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
  Amendment approved 2026-10-23 (INR-only display): every price shown or filtered is in INR. A source price in another currency is converted only with the documented, versioned rate in `config/exchange_rates.toml` (official source, rate date and provenance recorded; the original amount is kept in the database for traceability and never shown); a currency without a valid rate is "Price unavailable". Conversions are labelled as conversions of the source price, never as retailer INR prices, and stay separate from synthetic estimates.
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
