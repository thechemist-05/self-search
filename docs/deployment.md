# Deployment

*One-command setup and run instructions are due in Phase 11. This page currently covers deployment security.*

## Network exposure (reviewed 2026-10-19)

Self Search is a **local, single-user academic prototype**. By default every launch method is reachable from this machine only.

| Launch method | Binds to | Reachable from the LAN? |
|---|---|---|
| `.\venv\Scripts\python.exe -m uvicorn src.api.app:app` | `127.0.0.1` (uvicorn default; `api_host` setting) | **No.** Verified: connected on `127.0.0.1:8767`, refused on LAN address `192.168.1.33:8767` |
| `docker compose up` | Host `127.0.0.1:8000` (API) and `127.0.0.1:8501` (UI) | **No.** The published ports are loopback-only. Enforced by `tests/unit/test_deployment_config.py` (Docker is not installed on the dev machine, so this is verified statically) |
| `python -m streamlit run frontend/app.py` | `127.0.0.1:8501` (`.streamlit/config.toml`) | **No.** Verified: connected on `127.0.0.1:8771`, refused on `192.168.1.33:8771` |
| `docker run -p 8000:8000 ...` (manual) | **All host interfaces** | **Yes.** Use `-p 127.0.0.1:8000:8000` instead |

**Streamlit UI (Phase 9).** Usage telemetry is disabled (`browser.gatherUsageStats = false`). The UI's server process holds `SHELFSENSE_API_KEY` and adds it to API calls; the key is never rendered into the page (tested). Under compose, the UI reaches the API at `http://api:8000` on the compose network, and only `127.0.0.1:8501` is published on the host.

Inside the container uvicorn listens on `0.0.0.0`. That is the container's own interface and is required for port publishing to work; the publish rule decides host exposure.

## API key

- If the `API_KEY` environment variable is set, every endpoint except `GET /health` requires the header `X-API-Key`. The comparison is constant-time (`hmac.compare_digest`).
- **Unset by default** for local development (`API_KEY=${API_KEY:-}` in compose).
- `GET /health` is always open. It reveals only database and Ollama availability and the number of active sessions.

**Before making Self Search reachable from any other machine** (binding to a LAN interface, `0.0.0.0`, or a reverse proxy):

1. Set a long random `API_KEY`, e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Bind to a specific trusted interface rather than `0.0.0.0`, and keep the host firewall on.
3. Put TLS in front of it (the key is sent in a header in plain HTTP otherwise).
4. Understand the remaining limitations below. They are **not** fixed by the API key.

## Vector indexes must match the database

The API refuses to start, and uvicorn exits with `vector_index_manifest_invalid` in the log, when `manifest.json` in `SHELFSENSE_VECTOR_STORE_DIR` (default `data/vector_store`) is missing or does not match `SHELFSENSE_DB_PATH`. This stops it from serving products found through an index built from another catalogue.

| Situation | Fix |
|---|---|
| Existing indexes, no manifest | `python scripts/verify_index_manifest.py --check`, then `--write` if it reports consistent |
| Catalogue changed, or indexes rebuilt elsewhere | `python scripts/generate_embeddings.py --overwrite` (validated rebuild, atomic swap) |
| Serving a different database | Point `SHELFSENSE_VECTOR_STORE_DIR` at the indexes built from it |

Rows in the unused profile tables (users, preferences, wishlist, feedback) never affect the manifest. Under Docker, `manifest.json` must be in the mounted index directory alongside the index files.

## Final demo configuration (updated 2026-10-23)

The demo uses the **expanded catalogue, selection v4**: 86,588 products and 531,796 reviews from Amazon Reviews 2023, with Electronics excluded. It lives in `data/experiments/fashion-v4-20261023T014937Z/` together with its own manifest-checked indexes, `product_images.json` (validated photo URLs) and `dataset_manifest.json`. Every price is shown in INR, converted at the documented rate in `config/exchange_rates.toml`.
- **The previous catalogues** are kept unchanged with their own indexes:
  - selection v3: 50,000 products, `data/experiments/fashion-v3-20261022T085353Z/`;
  - selection v2: 20,000 products, `data/experiments/fashion-v2-20261021T094306Z/`.

  Start one with `run_demo.ps1 -Catalogue <name>`. The v4 database refuses the v3 indexes, and vice versa.
- **The small production database** (`data/processed/shelfsense.db`, 20 products, schema v4) is kept separate and unchanged.
- **Each catalogue directory holds exactly one database and its own `vector_store/`.** A database is never paired with another catalogue's indexes: the script derives both paths from one `-Catalogue` name, and the manifest check refuses a mismatch.

The two are not merged, because the FAISS ids are database row ids. The app selects a catalogue through the documented setting pair `SHELFSENSE_DB_PATH` + `SHELFSENSE_VECTOR_STORE_DIR`, and the startup manifest check refuses any mismatched pairing.

**Start both services:**
```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_demo.ps1
```
The script:
1. verifies the manifest;
2. sets the variables, including `SHELFSENSE_ESTIMATED_PRICES_ENABLED=true` (pass `-NoEstimates` to switch estimates off) and `SHELFSENSE_LOAD_REMOTE_IMAGES=true` (pass `-NoImages` to switch photos off);
3. starts the API on 8000 and the UI on 8502, writing logs to `%TEMP%\shelfsense-demo`.

Stop anything already listening on those ports first. Because Streamlit keeps imported modules in memory, **restart the UI after every code change**. A stale UI process mixes old and new code (the 2026-10-22 `render_turn` crash).

**Latency settings:**
- `SHELFSENSE_OLLAMA_TIMEOUT` (110 s) is the model-time budget per message, and it is below the UI's 120 s request timeout. A slow model therefore produces the labelled evidence-based fallback, not a UI error.
- If the UI's request does time out, the UI says so ("took too long"), not "API not reachable".

## Running the experimental Fashion catalogue (dataset phase 4)

The experimental build lives entirely in `data/experiments/<run-id>/`: `shelfsense.db` (schema v3, with `price_estimates`), `vector_store/` (with its own manifest) and `ingest_report.json`.

**Requirements:**
- The production database and indexes are never used by it.
- Set `SHELFSENSE_DB_PATH` and `SHELFSENSE_VECTOR_STORE_DIR` together. With any other pairing, the manifest check stops the API.
- Synthetic INR estimates stay off unless `SHELFSENSE_ESTIMATED_PRICES_ENABLED=true`.

**Rebuild from the verified raw files** (the download is in `data/raw/amazon_reviews_2023/<revision>/`):

```
python scripts/build_experimental_db.py --run-id <new-run-id>
SHELFSENSE_DB_PATH=data/experiments/<run-id>/shelfsense.db SHELFSENSE_VECTOR_STORE_DIR=data/experiments/<run-id>/vector_store EMBED_BATCH_SIZE=20000 python scripts/generate_embeddings.py
python scripts/verify_index_manifest.py --check --db data/experiments/<run-id>/shelfsense.db --index-dir data/experiments/<run-id>/vector_store
```

**Rollback:** stop the experimental processes and start the API without these variables, which uses the production defaults. No production file is changed by the experimental build.

## Remaining limitations (not addressed by this review)

- **One shared API key, no user accounts.** There are no profiles or per-user data (removed 2026-10-21); the optional `API_KEY` is the only access control.
- **Session IDs are bearer tokens.** Anyone who knows a session ID can continue that conversation.
- **The Streamlit UI has no authentication.** If its port is exposed, anyone reaching it can use the API through it.
- **No rate limiting or audit log.**

These are acceptable only for local use. Making Self Search multi-user would require real authentication and per-user authorisation, which is a design change for a future phase.
