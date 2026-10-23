"""
SQLite schema definition for Self Search.

Contains the complete DDL for all tables defined in the master spec Section 8,
with proper types, constraints, indexes, and FTS5 virtual tables for full-text search.
"""

from __future__ import annotations

# Schema version — increment when adding a step to MIGRATIONS below.
# Version 3 is reserved for the opt-in experimental price_estimates step
# (src/db/experimental_schema.py), which is deliberately not in MIGRATIONS.
SCHEMA_VERSION = 4

# Tables of the removed user-profile feature (2026-10-21). Created by the v1 baseline
# and step 2, dropped by step 4. Children first, so no foreign key ever dangles.
PROFILE_TABLES: tuple[str, ...] = (
    "messages", "conversations", "feedback", "wishlist", "user_preferences", "users",
)

TABLES_DDL: list[str] = [
    # -- Schema versioning --
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,

    # -- Data provenance --
    """
    CREATE TABLE IF NOT EXISTS data_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        url TEXT,
        license TEXT,
        downloaded_at TEXT,
        notes TEXT
    )
    """,

    # -- Product taxonomy --
    """
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        parent_id INTEGER REFERENCES categories(id)
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS brands (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    )
    """,

    # -- Products --
    """
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL REFERENCES data_sources(id),
        external_id TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        category_id INTEGER REFERENCES categories(id),
        brand_id INTEGER REFERENCES brands(id),
        price REAL,
        currency TEXT DEFAULT 'USD',
        avg_rating REAL,
        rating_count INTEGER DEFAULT 0,
        image_url TEXT,
        attributes_json TEXT,
        UNIQUE(source_id, external_id)
    )
    """,

    # -- Reviews --
    """
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL REFERENCES products(id),
        source_id INTEGER NOT NULL REFERENCES data_sources(id),
        external_id TEXT,
        rating REAL,
        title TEXT,
        text TEXT,
        review_date TEXT,
        helpful_votes INTEGER DEFAULT 0,
        verified_flag INTEGER DEFAULT 0,
        UNIQUE(source_id, external_id)
    )
    """,

    # -- Review analysis (Phase 2) --
    """
    CREATE TABLE IF NOT EXISTS review_analysis (
        review_id INTEGER PRIMARY KEY REFERENCES reviews(id),
        aspects_json TEXT,
        sentiment TEXT,
        complaints_json TEXT,
        topics_json TEXT,
        confidence REAL
    )
    """,

    # -- Product aspect summaries (Phase 2) --
    """
    CREATE TABLE IF NOT EXISTS product_aspect_summary (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL REFERENCES products(id),
        aspect TEXT NOT NULL,
        positive_count INTEGER DEFAULT 0,
        negative_count INTEGER DEFAULT 0,
        neutral_count INTEGER DEFAULT 0,
        UNIQUE(product_id, aspect)
    )
    """,

    # -- Users and personalisation (Phase 8; dropped by step 4) --
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        display_name TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS user_preferences (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        key TEXT NOT NULL,
        value TEXT,
        UNIQUE(user_id, key)
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS wishlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        product_id INTEGER NOT NULL REFERENCES products(id),
        added_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(user_id, product_id)
    )
    """,

    # -- Conversations and messages (Phase 7; never used, dropped by step 4) --
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        started_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id),
        role TEXT NOT NULL,
        content TEXT,
        structured_json TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,

    # -- Query logging --
    """
    CREATE TABLE IF NOT EXISTS query_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query TEXT,
        parsed_json TEXT,
        retrieved_ids_json TEXT,
        latency_json TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
]

INDEXES_DDL: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_products_source ON products(source_id)",
    "CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id)",
    "CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand_id)",
    "CREATE INDEX IF NOT EXISTS idx_products_price ON products(price)",
    "CREATE INDEX IF NOT EXISTS idx_products_rating ON products(avg_rating)",
    "CREATE INDEX IF NOT EXISTS idx_products_external ON products(external_id)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_product ON reviews(product_id)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_source ON reviews(source_id)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_rating ON reviews(rating)",
    "CREATE INDEX IF NOT EXISTS idx_review_analysis_review ON review_analysis(review_id)",
    "CREATE INDEX IF NOT EXISTS idx_product_aspect_product ON product_aspect_summary(product_id)",
    "CREATE INDEX IF NOT EXISTS idx_wishlist_user ON wishlist(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id)",
]

# FTS5 virtual tables for full-text search (used in Phase 4 for BM25 keyword retrieval)
FTS5_DDL: list[str] = [
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(
        title, description, attributes_json,
        content='products',
        content_rowid='id'
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS reviews_fts USING fts5(
        title, text,
        content='reviews',
        content_rowid='id'
    )
    """,
]

# Triggers to keep FTS indexes in sync with the main tables
FTS5_TRIGGERS_DDL: list[str] = [
    # Products FTS triggers
    """
    CREATE TRIGGER IF NOT EXISTS products_ai AFTER INSERT ON products BEGIN
        INSERT INTO products_fts(rowid, title, description, attributes_json)
        VALUES (new.id, new.title, new.description, new.attributes_json);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS products_ad AFTER DELETE ON products BEGIN
        INSERT INTO products_fts(products_fts, rowid, title, description, attributes_json)
        VALUES ('delete', old.id, old.title, old.description, old.attributes_json);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS products_au AFTER UPDATE ON products BEGIN
        INSERT INTO products_fts(products_fts, rowid, title, description, attributes_json)
        VALUES ('delete', old.id, old.title, old.description, old.attributes_json);
        INSERT INTO products_fts(rowid, title, description, attributes_json)
        VALUES (new.id, new.title, new.description, new.attributes_json);
    END
    """,
    # Reviews FTS triggers
    """
    CREATE TRIGGER IF NOT EXISTS reviews_ai AFTER INSERT ON reviews BEGIN
        INSERT INTO reviews_fts(rowid, title, text)
        VALUES (new.id, new.title, new.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS reviews_ad AFTER DELETE ON reviews BEGIN
        INSERT INTO reviews_fts(reviews_fts, rowid, title, text)
        VALUES ('delete', old.id, old.title, old.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS reviews_au AFTER UPDATE ON reviews BEGIN
        INSERT INTO reviews_fts(reviews_fts, rowid, title, text)
        VALUES ('delete', old.id, old.title, old.text);
        INSERT INTO reviews_fts(rowid, title, text)
        VALUES (new.id, new.title, new.text);
    END
    """,
]

# Version 1 baseline. Later versions are applied as incremental steps from MIGRATIONS.
ALL_DDL = TABLES_DDL + INDEXES_DDL + FTS5_DDL + FTS5_TRIGGERS_DDL

# Incremental migrations: target version -> statements. Each step runs inside a
# single transaction. Steps are history: never edit one that has shipped.
MIGRATIONS: dict[int, list[str]] = {
    # -- Phase 8: personal memory --
    2: [
        # Consent is opt-in: personalisation is off until the user enables it.
        "ALTER TABLE users ADD COLUMN personalization_consent INTEGER NOT NULL DEFAULT 0 "
        "CHECK (personalization_consent IN (0, 1))",
        "ALTER TABLE users ADD COLUMN updated_at TEXT",
        # Only explicit, API-set preferences may be stored; inferred values are
        # rejected by the database itself.
        "ALTER TABLE user_preferences ADD COLUMN source TEXT NOT NULL DEFAULT 'user_explicit' "
        "CHECK (source IN ('user_explicit'))",
        "ALTER TABLE user_preferences ADD COLUMN updated_at TEXT",
        """
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            signal TEXT NOT NULL CHECK (signal IN ('up', 'down')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(user_id, product_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_user_preferences_user ON user_preferences(user_id)",
    ],
    # -- Remove user profiles (2026-10-21) --
    # Destructive, so guarded: the step refuses to run unless every table is empty
    # (REQUIRE_EMPTY below). Their indexes are dropped with them.
    4: [f"DROP TABLE IF EXISTS {table}" for table in PROFILE_TABLES],
}

# Tables that must be empty before a step runs; otherwise the step is refused and
# the database is left unchanged (export the rows first).
REQUIRE_EMPTY: dict[int, tuple[str, ...]] = {4: PROFILE_TABLES}

# Tables expected to exist after migration (for verification)
EXPECTED_TABLES = [
    "schema_version",
    "data_sources",
    "categories",
    "brands",
    "products",
    "reviews",
    "review_analysis",
    "product_aspect_summary",
    "query_logs",
    "products_fts",
    "reviews_fts",
]
