# Cleanup: Obsolete Profile Tables (schema step 4)

User profiles were removed from the application code on 2026-10-21. Step 4 of the schema migrations drops their tables.

**Status: APPLIED to `data/processed/shelfsense.db` on 2026-10-21 at 20:59 UTC** (v2 → v4), with approval.

**Before applying:**
- The API on port 8000 was stopped.
- A fresh dry run was clean.
- All six tables had 0 rows.
- The verified backup `data/processed/shelfsense.v2.20261021T205908Z.premigration.bak.db` passed `integrity_check`, and its logical dump was identical to the source.
- `migrate_db.py` also wrote `data/processed/shelfsense.v2.20261021T205933Z.bak.db`.
- Both backups have sha256 `33d439e08b1795906e982f995e4c936958c494f9eb23dce4ca975257546b40c2`.

**After applying:**
- `integrity_check` ok; `foreign_key_check` empty; schema versions 1, 2, 4.
- No profile tables or indexes are left.
- Every other table is identical in row count and content hash (20 products, 100 reviews).
- The catalogue fingerprint is unchanged, and `verify_index_manifest.py --check` is consistent.
- The index files and `manifest.json` are byte-identical to before.
- The new database sha256 is `94174218bd71fd714040e38394b5ae18dc7c1540504ea04c62fa6cc27b48540e`.

The sections below describe the state and the rehearsal **before** the migration.

## Target database (verified read-only, 2026-10-21)

| | |
|---|---|
| Path | `data/processed/shelfsense.db` (absolute: `C:\Users\KIIT0001\.gemini\antigravity-ide\scratch\shelfsense\data\processed\shelfsense.db`) |
| Why this one | the settings default; there is no `.env` and `SHELFSENSE_DB_PATH` is not set. It is also the database the API on port 8000 serves. |
| Schema version | 2 (versions 1 and 2 recorded); no `price_estimates` |
| Catalogue | 20 products, 100 reviews, 2 categories, 20 brands, 0 query logs |
| Health | journal mode WAL; `integrity_check` ok; `foreign_key_check` empty |
| sha256 (main file) | `17d9b2d7ee52e185689afa844b440647c645361410f347ba3b2c9f7bcb8a780c` |

| Table | Rows | Read or written by code? |
|---|---|---|
| `users` | 0 | no |
| `user_preferences` | 0 | no |
| `wishlist` | 0 | no |
| `feedback` | 0 | no |
| `conversations` | 0 | no (never used; `user_id` references `users`) |
| `messages` | 0 | no (never used; references `conversations`) |

**Dependencies checked:**
- *Foreign keys:* only from these tables, to `users`, `conversations` and `products`. **No other table references them.**
- *Indexes:* `idx_wishlist_user`, `idx_feedback_user`, `idx_user_preferences_user`, `idx_messages_conversation`, plus three automatic UNIQUE indexes. All are dropped with their tables.
- *Triggers and views:* none on these tables. The FTS triggers are on `products` and `reviews` only.
- *Code:* none outside `src/db/schema.py` (DDL history). The API, retrieval, budget filtering, estimates, evidence validation, health checks and security never touch them.
- *Index manifest:* the fingerprint covers products and reviews only, so the step cannot invalidate any index. Rehearsal confirmed this: the fingerprint was unchanged and `require_valid_manifest` passed against `data/vector_store`.
- *Startup:* the API **never runs migrations**. Only `scripts/migrate_db.py`, `scripts/ingest_data.py` and `scripts/build_experimental_db.py` call `run_migrations`.

## What step 4 does (`src/db/schema.py`)
- `DROP TABLE IF EXISTS` for `messages`, `conversations`, `feedback`, `wishlist`, `user_preferences`, `users`, children first.
- **Guarded:** inside the same transaction it first counts the rows of all six tables. If any holds rows it raises `MigrationRefusedError` and rolls back, so **nothing is changed**. This also protects any later `ingest_data.py` run.
- One transaction: a failure part-way rolls back every drop (tested).
- Steps 1 and 2 stay unchanged as history, and new databases go v1 → v2 → v4. `EXPECTED_TABLES` no longer lists the six tables.
- Experimental databases built before step 4 (v2 plus the v3 `price_estimates` marker) keep `price_estimates` and its rows (tested).

## Rehearsal results
1. **Production, dry run:** `scripts/migrate_db.py --dry-run` copies with the backup API from a read-only connection.
   - Result: 0 rows in all six tables; v2 → v4; no profile tables left; `integrity_check` ok; `foreign_key_check` empty; catalogue counts unchanged.
   - The sha256 of the production database, its WAL file and the v1 backup were identical before and after.
2. **Fashion experimental database** (`data/experiments/fashion-v2-20261021T094306Z/shelfsense.db`; a copy, **not a target**): the step was **refused**, because `users` holds 1 row (id 14, "SAM", created 2026-10-21 13:26, consent 0), apparently a profile created in the UI during testing. The guard worked as designed. That database is unchanged, and it will not be migrated without a separate decision about that row.

## Procedure (when approved)
1. **Stop** the API on port 8000, which holds the database open. The Streamlit UI on 8502 can stay up, but chats fail while the API is down.
2. **Re-check:**
   ```
   .\venv\Scripts\python.exe scripts/migrate_db.py --dry-run
   ```
   Expect `Profile rows: {... all 0 ...}` and `v2 -> v4`. If any count is not 0, **stop** and export those rows first.
3. **Apply:**
   ```
   .\venv\Scripts\python.exe scripts/migrate_db.py --db-path data/processed/shelfsense.db
   ```
   - It first writes a consistent backup with SQLite's online backup API to `data/processed/shelfsense.v2.<UTC>.bak.db`, then applies step 4 in one transaction.
   - It then checks integrity, foreign keys and catalogue counts. Exit code 1 means a problem, and the script prints the backup path.
4. **Verify:**
   - record the backup's sha256;
   - `PRAGMA integrity_check` is ok;
   - `.\venv\Scripts\python.exe scripts/verify_index_manifest.py --check` passes;
   - the full test suite passes.
5. **Restart** the API and check that `/health` returns `ok`.

## Rollback and recovery
- **Before or during the step:** nothing to do. A refused or failed step rolls back completely and the database stays at v2.
- **After the step:** stop the API, delete `shelfsense.db-wal` and `shelfsense.db-shm` if present, then copy `shelfsense.v2.<UTC>.bak.db` over `shelfsense.db` and restart. The backup is a complete, consistent copy, so this restores the exact prior state, including the empty profile tables.
- **Old code against a v4 database:** it still works, because no code reads the dropped tables. `verify_schema` from an older checkout would report them missing.

## Not included
- The v1 backup (`shelfsense.v1.20261019T092541Z.bak.db`) and every other backup are left as they are.
- Experimental databases are not targeted. `migrate_db.py` refuses them without `--allow-experimental`.
