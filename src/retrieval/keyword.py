"""
SQLite FTS5 keyword retrieval for Self Search (BM25).

Provides full-text keyword search over the products_fts virtual table,
with query sanitization, BM25 scoring, and optional metadata filtering.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Optional

from src.retrieval.constraints import singular
from src.retrieval.filters import ESTIMATE_KEY, MetadataFilter, strip_estimates

# When product-type/exclusion constraints are set (checked in Python after SQL),
# fetch this many rows before filtering so valid products are not cut off.
# Rows fetched before Python-only constraints (strict type, colour, material, brand) are
# applied; large enough that strict filters still find matches deep in a 50,000-product catalogue.
TEXT_CONSTRAINT_OVERFETCH = 2000


# Words that carry no product meaning in a shopping query. Without this list,
# "a" in "I need a shoe" matched the "A" of "USB-A" and ranked a power bank first.
STOPWORDS = frozenset("""
a an the and or but of in on at to for from with without by as is are was be been being it its this that
these those i im me my we our you your he she they them which who what when where how can could would should
will shall may might must do does did need needs want wants looking look find show give get buy some something
anything any good best great nice really very just only also please use used using everyday daily
under below above over between less more than upto up price priced cost costs budget cheap cheaper expensive
rupee rupees rs inr usd dollar dollars euro euros eur pound pounds gbp star stars rated rating ratings
""".split())


def _prefix_stem(token: str) -> str:
    """Plural-insensitive prefix: shoes -> shoe, batteries -> batter (matches battery/batteries)."""
    stem = singular(token)
    return stem[:-1] if len(stem) > 4 and stem.endswith("y") else stem


def sanitize_fts5_query(query: str, match_all: bool = False) -> str:
    """Sanitize and prepare a user query string for SQLite FTS5 MATCH.

    Extracts alphanumeric tokens, drops stopwords, tokens shorter than 3
    characters and bare numbers, and joins the rest with OR so BM25 ranks
    documents matching more terms higher. Tokens of 4+ characters become
    plural-insensitive prefix queries ("shoe" matches "Shoes").
    Every token is quoted to avoid FTS5 reserved words (AND, OR, NOT).
    With ``match_all`` the tokens are joined with AND instead (every word required).
    Returns an empty string if no meaningful tokens remain.
    """
    if not query or not query.strip():
        return ""

    terms: list[str] = []
    for tok in re.findall(r"[A-Za-z0-9]+", query):
        low = tok.lower()
        if len(low) < 3 or low.isdigit() or low in STOPWORDS:
            continue
        term = f'"{_prefix_stem(low)}"*' if len(low) >= 4 else f'"{low}"'
        if term not in terms:
            terms.append(term)
    return (" AND " if match_all else " OR ").join(terms)


def keyword_search_products(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    filters: Optional[MetadataFilter] = None,
    match_all: bool = False,
) -> list[dict[str, Any]]:
    """Search products using SQLite FTS5 BM25.

    Args:
        conn: SQLite database connection.
        query: Raw query string.
        limit: Maximum number of candidate products to return.
        filters: Optional metadata filters to constrain the candidate set.
        match_all: Require every query word (AND) instead of any (OR).

    Returns:
        List of product dicts ordered by relevance (best match first), each
        containing product fields and a 'score' (positive BM25 score).
    """
    fts_query = sanitize_fts5_query(query, match_all=match_all)
    if not fts_query:
        return []

    where_clauses = ["products_fts MATCH ?"]
    params: list[Any] = [fts_query]

    if filters and not filters.is_empty():
        filter_sql, filter_params = filters.build_sql_conditions(table_prefix="p")
        if filter_sql:
            where_clauses.append(filter_sql)
            params.extend(filter_params)

    where_str = " AND ".join(where_clauses)
    estimate_col = filters.estimate_column("p") if filters else None
    estimate_select = f", {estimate_col} AS price_estimate_inr" if estimate_col else ""

    sql = f"""
        SELECT
            p.id,
            p.source_id,
            p.external_id,
            p.title,
            p.description,
            p.category_id,
            p.brand_id,
            p.price,
            p.currency,
            p.avg_rating,
            p.rating_count,
            p.image_url,
            p.attributes_json,
            c.name AS category_name,
            b.name AS brand_name,
            bm25(products_fts) AS raw_bm25{estimate_select}
        FROM products_fts
        JOIN products p ON products_fts.rowid = p.id
        LEFT JOIN categories c ON p.category_id = c.id
        LEFT JOIN brands b ON p.brand_id = b.id
        WHERE {where_str}
        ORDER BY raw_bm25 ASC
        LIMIT ?
    """
    text_constraints = bool(filters and filters.has_text_constraints())
    params.append(max(limit, TEXT_CONSTRAINT_OVERFETCH) if text_constraints else limit)

    cursor = conn.cursor()
    rows = cursor.execute(sql, params).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        raw_bm25 = row[15]
        # In SQLite FTS5, bm25 returns negative values (more negative = better match).
        # We invert it to positive so higher score indicates stronger relevance.
        score = -float(raw_bm25) if raw_bm25 is not None else 0.0
        results.append({
            "id": row[0],
            "source_id": row[1],
            "external_id": row[2],
            "title": row[3],
            "description": row[4],
            "category_id": row[5],
            "brand_id": row[6],
            "price": row[7],
            "currency": row[8],
            "avg_rating": row[9],
            "rating_count": row[10],
            "image_url": row[11],
            "attributes_json": row[12],
            "category_name": row[13],
            "brand_name": row[14],
            "score": score,
            "retrieval_source": "keyword",
        })
        if estimate_col:
            results[-1][ESTIMATE_KEY] = row[16]

    if text_constraints:
        # Product-type and exclusion constraints are enforced here, before ranking/fusion.
        results = [r for r in results if filters.matches(r)][:limit]  # type: ignore[union-attr]

    return strip_estimates(results)
