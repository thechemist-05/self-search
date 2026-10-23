# Database Schema

> See [master-spec.md](master-spec.md) Section 8 for the starting schema. The authoritative DDL is in
> `src/db/schema.py`.

*The full column-by-column reference for the version 1 tables (catalogue, reviews, analysis, FTS) is
still outstanding from Phase 1. This document currently covers versioning, migrations, and the
Phase 8 personal-memory tables.*

## Versioning and migrations

| Version | Phase | Change |
|---|---|---|
| 1 | Phase 1 | Baseline: all tables in spec §8, indexes, FTS5 tables and triggers |
| 2 | Phase 8 | Personal memory: consent and timestamps on `users`; source and timestamp on `user_preferences`; new `feedback` table; 2 indexes |
| 3 (experimental) | Dataset phase 2 | `price_estimates` table for synthetic INR estimates. **Experimental databases only**, applied explicitly. Not in `MIGRATIONS` (see below). |
| 4 | Profile removal (2026-10-21) | Drops the six profile tables `messages`, `conversations`, `feedback`, `wishlist`, `user_preferences`, `users` (and their indexes). **Guarded:** refused, with nothing changed, if any of them holds rows. See [profile-tables-cleanup.md](profile-tables-cleanup.md). |

`src/db/migrations.py::run_migrations(conn)`:

- A fresh database gets the v1 baseline (`ALL_DDL`), then each step in `MIGRATIONS` in ascending order.
- An existing database gets only the steps above its recorded `schema_version`. `run_migrations(conn, target_version=N)` stops after version N (used by tests of earlier steps).
- **Each step runs in one transaction.** If any statement fails, the whole step rolls back and the database stays at the previous version. This is tested by injecting a failing statement.
- Steps 1–2 are additive (`ADD COLUMN`, `CREATE TABLE`, `CREATE INDEX`). Step 4 is the only destructive step. It runs only when every table it drops is empty (`REQUIRE_EMPTY` in `src/db/schema.py`); otherwise it raises `MigrationRefusedError` and rolls back. Shipped steps are history and are never edited, so a fresh database still goes v1 → v2 → v4.
- Re-running on a current database is a no-op.

**Upgrading a real database.** Always use the script, which takes a consistent backup first through SQLite's online backup API:

```
.\venv\Scripts\python.exe scripts/migrate_db.py [--db-path data/processed/shelfsense.db]
```

The backup is written beside the database as `<name>.v<old_version>.<UTC timestamp>.bak.db` (git-ignored via `*.db`). `--dry-run` rehearses on a temporary copy and leaves the database untouched. After migrating, the script checks `integrity_check`, `foreign_key_check` and that catalogue row counts are unchanged. Databases under `data/experiments/` are refused unless `--allow-experimental` is given.

The API server **never migrates implicitly**. On a v1 database it starts normally, with all Phase 7 features working, and the (now removed) `/users` endpoints and `/chat` with a `user_id` return `503` with instructions to run the script.

## Experimental schema v3: `price_estimates`

Applied **only** by an explicit `src/db/experimental_schema.py::apply_experimental_v3(conn)` call on an experimental database at the current `SCHEMA_VERSION` (4).
- **Kept out of the normal upgrade path.** It is deliberately not in `MIGRATIONS`, so `run_migrations` and `scripts/migrate_db.py` never add it.
- **Version 3 is a marker row** in `schema_version`; the current version stays `SCHEMA_VERSION`.
- **Older experimental databases** (v2 plus v3, built before step 4) keep `price_estimates` and its rows when `run_migrations` upgrades them, because step 4 drops only the profile tables.
- **Refused targets:** the configured production database, any `*.db` in `data/processed/` (which covers the v1 backup), in-memory databases, and any other version.
- **Re-applying** is a no-op when the table and the v3 marker both exist. A half-applied state (only one of the two) is refused.
- Budget filtering and estimates detect the table itself (`has_price_estimates`), not the version number.

| Column | Notes |
|---|---|
| `product_id` | Primary key, references `products(id)`, `ON DELETE CASCADE` |
| `currency` | `CHECK (currency = 'INR')` |
| `amount`, `range_min`, `range_max` | positive; `range_min < range_max`; `amount` inside the range |
| `rule_key`, `method`, `config_version`, `config_sha256` | which rule and which exact config produced the value |
| `label` | `CHECK (label = 'synthetic_estimate')` |
| `created_at` | |

**Estimates never touch `products.price` or `products.currency`.** Those columns keep meaning "price supplied by the source". The evidence validator reads only `products.price`, so an estimate can never verify a price claim.

**Applied to the project database on 2026-10-19:** `data/processed/shelfsense.db` v1 → v2. The backup is `data/processed/shelfsense.v1.20261019T092541Z.bak.db`. Afterwards both files pass `PRAGMA integrity_check`, and both have 20 products, 100 reviews and 0 users.

## Personal-memory tables (schema v2): obsolete since 2026-10-21, dropped by step 4

User profiles were removed from the application. These tables are **no longer read or written** by any code. New databases no longer keep them: step 4 drops them, and they are not in `EXPECTED_TABLES`. Existing databases keep them until step 4 is applied with `scripts/migrate_db.py`.

Nothing else depends on them: the foreign keys point *from* `user_preferences`, `wishlist`, `feedback` and `conversations` *to* `users` and `products`, never the other way. The index manifest fingerprint excludes them.

In the project database all six tables were empty (0 rows) on 2026-10-21, and **step 4 was applied there on 2026-10-21** (v2 → v4). The backup is `data/processed/shelfsense.v2.20261021T205908Z.premigration.bak.db`. Details are in [profile-tables-cleanup.md](profile-tables-cleanup.md).

The descriptions below are kept for reference.


Rows here are created **only** by explicit `/users/...` API calls. Chat never writes to them.

### `users`
| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | An identifier, **not** a credential |
| `display_name` | TEXT | Optional, ≤100 characters (validated in the service) |
| `created_at` | TEXT | Default `datetime('now')` |
| `personalization_consent` | INTEGER NOT NULL DEFAULT 0 | `CHECK IN (0,1)`. Opt-in; 0 means personal ranking is never applied (v2) |
| `updated_at` | TEXT | Set on create and on consent change (v2) |

### `user_preferences`
| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | Referenced by personalisation explanations |
| `user_id` | INTEGER FK → users | |
| `key` | TEXT | `UNIQUE(user_id, key)`. The service allows only `preferred_brands`, `avoided_brands`, `preferred_categories`, `budget_max`, `min_rating` |
| `value` | TEXT | JSON. Validated per key; malformed rows are ignored when read |
| `source` | TEXT NOT NULL DEFAULT `'user_explicit'` | `CHECK (source IN ('user_explicit'))`. **The database rejects any other source**, so inferred preferences cannot be stored (v2) |
| `updated_at` | TEXT | (v2) |

### `wishlist` (unchanged from v1)
`id`, `user_id` FK, `product_id` FK → products, `added_at`, `UNIQUE(user_id, product_id)`.

### `feedback` (new in v2)
| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | Referenced by explanations |
| `user_id` | INTEGER NOT NULL FK → users | |
| `product_id` | INTEGER NOT NULL FK → products | |
| `signal` | TEXT NOT NULL | `CHECK (signal IN ('up','down'))` |
| `created_at` | TEXT | Updated when the vote changes |
| | | `UNIQUE(user_id, product_id)`: one vote per product, the latest wins |

### `conversations`, `messages`, `query_logs`
These exist since v1 and are **intentionally unused** in Phase 8: no conversation history, messages or queries are persisted. User deletion still clears any `conversations` and `messages` rows linked to the user. `query_logs` has no user column.

## Deletion

`SQLitePersonalMemoryRepository.delete_user_data` deletes, inside one SAVEPOINT, in child-before-parent order: `feedback` → `wishlist` → `user_preferences` → `messages` (of the user's conversations) → `conversations` → `users`. Any failure rolls back every deletion; this is tested with a trigger that aborts on the final `users` delete. Catalogue rows are never touched.
