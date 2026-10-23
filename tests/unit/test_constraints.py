"""
Regression tests for the relevance/constraint fix (Phase 9).

Observed bug: "I need a shoe under 4000 rupees, which can be everyday use,
sneakers." returned a power bank, a laptop stand and other electronics; the
rupee budget was silently compared with USD prices.
"""

from __future__ import annotations

import random
import shutil
import sqlite3
from pathlib import Path

import pytest

from src.agent.constraints import ConstraintResolver, constraint_checks
from src.agent.query_parser import parse_query_rule_based
from src.agent.schemas import ParsedQuery, UserIntent
from src.agent.session import SessionState
from src.retrieval.constraints import (
    detect_price_currency,
    detect_product_types,
    match_product_type,
    mentions_term,
)
from src.retrieval.filters import MetadataFilter

SHOE_QUERY = "I need a shoe under 4000 rupees, which can be everyday use, sneakers."
REAL_DB = Path("data/processed/shelfsense.db")


class TestCurrencyDetection:
    @pytest.mark.parametrize("text,code", [
        ("under ₹4000", "INR"), ("under Rs 4000", "INR"), ("under rs. 4000", "INR"),
        ("4000 INR", "INR"), ("under 4000 rupees", "INR"), ("1 rupee", "INR"),
        ("under $50", "USD"), ("50 dollars", "USD"), ("USD 50", "USD"),
        ("under €30", "EUR"), ("30 euros", "EUR"), ("under £30", "GBP"),
        ("under 4000", None), ("a 5 pound weight", None),
    ])
    def test_detect(self, text, code) -> None:
        assert detect_price_currency(text) == code


class TestProductTypes:
    @pytest.mark.parametrize("text,types", [
        # 2026-10-22 strict matching: footwear sub-types are detected separately
        (SHOE_QUERY, ["footwear", "sneakers"]),
        ("running shoes", ["running shoes"]), ("trainers for the gym", ["sneakers"]),
        ("footwear", ["footwear"]), ("laptop stand", ["laptop stand"]),
        ("wireless earbuds", ["earbuds"]), ("a gaming mouse", ["mouse"]),
        ("show me only black ones", []), ("coffee maker", []),
    ])
    def test_detect(self, text, types) -> None:
        assert detect_product_types(text) == types

    def test_matching_uses_title_and_description_words(self) -> None:
        shoe = {"title": "Marathon Running Shoes - Men", "description": "Lightweight running shoes"}
        socks = {"title": "Trail Running Socks Merino Wool", "description": "cushioned sole"}
        assert match_product_type(shoe, ("footwear",)) == ("footwear", "title", "shoe")
        assert match_product_type(socks, ("footwear",)) is None  # socks are not footwear

    def test_known_lexical_limitation_is_documented(self) -> None:
        # Word-based matching: "laptop" also matches "Laptop Stand". Documented in limitations.md.
        assert match_product_type({"title": "Laptop Stand Adjustable"}, ("laptop",)) is not None

    def test_term_matching_is_whole_word(self) -> None:
        mouse = {"title": "Wireless Gaming Mouse RGB", "description": ""}
        assert mentions_term(mouse, "wired") is None and mentions_term(mouse, "rgb") == "title"


class TestMetadataFilter:
    PRODUCT = {"id": 3, "title": "Marathon Running Shoes - Men", "description": "", "price": 89.99,
               "currency": "USD", "avg_rating": 4.5, "brand_name": "SpeedFit", "category_name": "Sports_and_Outdoors"}

    def test_price_bounds_use_the_inr_price(self) -> None:
        # USD 89.99 is ₹7,477 at the documented rate; budgets are INR only.
        assert MetadataFilter(max_price=7477, price_currency="INR").matches(self.PRODUCT)
        assert not MetadataFilter(max_price=7476, price_currency="INR").matches(self.PRODUCT)
        assert not MetadataFilter(max_price=100, price_currency="INR").matches(self.PRODUCT)  # never "89.99 rupees"
        assert not MetadataFilter(max_price=100000, price_currency="USD").matches(self.PRODUCT)

    def test_type_brand_and_term_exclusions(self) -> None:
        assert MetadataFilter(product_types=("footwear",)).matches(self.PRODUCT)
        assert not MetadataFilter(product_types=("keyboard",)).matches(self.PRODUCT)
        assert not MetadataFilter(excluded_brands=("speedfit",)).matches(self.PRODUCT)
        assert not MetadataFilter(excluded_terms=("running",)).matches(self.PRODUCT)
        assert MetadataFilter(excluded_terms=("rgb",)).matches(self.PRODUCT)

    def test_is_empty_counts_text_constraints(self) -> None:
        assert MetadataFilter().is_empty()
        assert not MetadataFilter(excluded_terms=("rgb",)).is_empty()


class TestParser:
    def test_observed_shoe_query(self) -> None:
        p = parse_query_rule_based(SHOE_QUERY)
        assert p.intent == UserIntent.PRODUCT_SEARCH
        assert (p.max_price, p.price_currency, p.product_types) == (4000.0, "INR", ["footwear", "sneakers"])
        assert "4000" not in p.search_text and "rupees" not in p.search_text

    @pytest.mark.parametrize("text,lo,hi,cur", [
        ("shoes under $100", None, 100.0, "USD"),
        ("shoes under Rs 4000", None, 4000.0, "INR"),
        ("shoes under ₹4000", None, 4000.0, "INR"),
        ("mouse between Rs 500 and Rs 2000", 500.0, 2000.0, "INR"),
        ("shoe under 4000", None, 4000.0, None),
    ])
    def test_price_and_currency(self, text, lo, hi, cur) -> None:
        p = parse_query_rule_based(text)
        assert (p.min_price, p.max_price, p.price_currency) == (lo, hi, cur)

    def test_llm_category_maps_to_product_type(self) -> None:
        from src.agent.query_parser import apply_deterministic_constraints

        p = apply_deterministic_constraints(ParsedQuery(intent=UserIntent.PRODUCT_SEARCH,
                                                        raw_query="something to run in", category="shoes"))
        assert p.product_types == ["footwear"]


class TestResolver:
    def _resolve(self, conn, text: str):
        r = ConstraintResolver(conn)
        p = parse_query_rule_based(text)
        res = r.resolve(p)
        return r, p, res, SessionState(session_id="s").merge_filters(p, res.fields)

    def test_rupee_budget_applies_to_inr_prices(self, personal_db) -> None:
        r, _, res, filters = self._resolve(personal_db, SHOE_QUERY)
        assert not res.price_blocked and filters.max_price == 4000 and filters.price_currency == "INR"
        assert filters.product_types == ("footwear", "sneakers")
        report = r.report(filters, res)
        price = next(i for i in report.items if i.kind == "price")
        assert price.status == "applied" and price.label.startswith("Max price: ₹4,000")
        assert "documented exchange rate" in price.label and "unknown prices never match" in price.label
        assert report.catalog_currencies == ["INR"]
        assert report.matching_products == 0  # the only shoe is USD 89.99 = ₹7,477

    def test_dollar_budget_is_not_converted_and_asks_for_rupees(self, personal_db) -> None:
        r, _, res, filters = self._resolve(personal_db, "trail shoes under $100")
        assert res.price_blocked and res.price_status == "not_applied_currency_mismatch"
        assert filters.max_price is None
        report = r.report(filters, res)
        price = next(i for i in report.items if i.kind == "price")
        assert price.status == "not_applied" and "rupees (₹)" in price.reason
        msg = r.clarification(res, report)
        assert "₹" in msg and "won't convert" in msg and "$" not in msg

    def test_unstated_currency_is_inr(self, personal_db) -> None:
        r, _, res, filters = self._resolve(personal_db, "shoe under 8000")
        assert not res.price_blocked and filters.price_currency == "INR"
        report = r.report(filters, res)
        price = next(i for i in report.items if i.kind == "price")
        assert price.label.startswith("Max price: ₹8,000") and "no currency given; all prices are in ₹" in price.label
        assert {p["id"] for p in r.matching_products(filters)} == {6}  # Trail Shoes: USD 89.99 = ₹7,477

    def test_currency_without_a_documented_rate_never_matches(self, personal_db) -> None:
        personal_db.execute("UPDATE products SET currency = 'EUR' WHERE id = 6")  # no EUR rate is configured
        r, _, _, filters = self._resolve(personal_db, "trail shoes under ₹1000000")
        assert {p["id"] for p in r.matching_products(filters)} == set()

    def test_llm_category_is_not_used_as_a_substring_category_filter(self, personal_db) -> None:
        r = ConstraintResolver(personal_db)
        p = ParsedQuery(intent=UserIntent.PRODUCT_SEARCH, raw_query="x", category="shoes",
                        product_types=["footwear"])
        res = r.resolve(p)
        assert "category" not in res.fields and res.fields["product_types"] == ("footwear",)
        exact = r.resolve(ParsedQuery(intent=UserIntent.PRODUCT_SEARCH, raw_query="x", category="electronics"))
        assert exact.fields["category"] == "Electronics"

    def test_exclusions_split_into_brands_and_terms(self, personal_db) -> None:
        _, _, res, _ = self._resolve(personal_db, "audio gear without RGB, not soundwave")
        assert res.fields["excluded_brands"] == ("SoundWave",)
        assert res.fields["excluded_terms"] == ("RGB",)

    def test_constraint_checks_cite_database_fields(self) -> None:
        f = MetadataFilter(product_types=("footwear",), max_price=10000.0, price_currency="INR",
                           excluded_terms=("rgb",))
        checks = constraint_checks({"title": "Marathon Running Shoes", "description": "", "price": 89.99,
                                    "currency": "USD"}, f)
        assert [c.detail for c in checks] == [
            "footwear: title mentions “shoe”",
            "₹7,477 ≤ ₹10,000 (converted from the source price at ₹83.08 per US dollar "
            "(ECB reference rate, 2023-09-29))",
            "Title and description don't mention “rgb”",
        ]
        assert not any("$" in c.detail or "USD" in c.detail for c in checks)


class TestSessionMerge:
    def test_refine_keeps_type_unions_exclusions_and_moves_currency_with_price(self) -> None:
        s = SessionState(session_id="s")
        s.merge_filters(ParsedQuery(intent=UserIntent.PRODUCT_SEARCH, raw_query="x"),
                        {"product_types": ("footwear",), "excluded_terms": ("rgb",)})
        f = s.merge_filters(ParsedQuery(intent=UserIntent.REFINE_SEARCH, raw_query="y"),
                            {"max_price": 50.0, "price_currency": "USD", "excluded_terms": ("leather",)})
        assert f.product_types == ("footwear",)
        assert f.excluded_terms == ("rgb", "leather")
        assert (f.max_price, f.price_currency) == (50.0, "USD")


@pytest.fixture(scope="module")
def real_db(tmp_path_factory):
    if not REAL_DB.exists():
        pytest.skip("data/processed/shelfsense.db not found")
    copy = tmp_path_factory.mktemp("relevance") / "shelfsense.db"
    shutil.copy(REAL_DB, copy)
    conn = sqlite3.connect(str(copy))
    yield conn
    conn.close()


class TestRetrievalNeverViolatesConstraints:
    """Hybrid/semantic/RRF ranking can never return a product that violates an applied constraint."""

    QUERIES = ["shoe", "wireless", "bluetooth speaker", "backpack", "gear", "light", "rgb keyboard",
               "battery", "fitness", "outdoor", "sneakers everyday", "power", "camera", "mat"]
    TYPES = [(), ("footwear",), ("keyboard",), ("speaker",), ("backpack",), ("earbuds",)]

    def test_seeded_random_constraint_sets_all_modes(self, real_db) -> None:
        from src.retrieval.hybrid import HybridRetriever

        retriever = HybridRetriever(conn=real_db)
        rng = random.Random(20261019)
        checked = 0
        for _ in range(60):
            f = MetadataFilter(
                product_types=rng.choice(self.TYPES),
                max_price=rng.choice([None, 20.0, 50.0, 100.0]),
                min_rating=rng.choice([None, 4.0, 4.5]),
                excluded_brands=rng.choice([(), ("SoundWave",), ("KeyCraft",)]),
                excluded_terms=rng.choice([(), ("rgb",), ("wireless",)]),
                price_currency="USD",
            )
            query = rng.choice(self.QUERIES)
            for mode in ("keyword", "semantic", "hybrid", "reranked_hybrid"):
                for p in retriever.retrieve(query, limit=10, filters=f, mode=mode):
                    assert f.matches(p), (mode, query, f, p["title"])
                    checked += 1
        assert checked > 0

    def test_observed_query_returns_no_electronics_or_over_budget(self, real_db) -> None:
        from src.retrieval.hybrid import HybridRetriever

        f = MetadataFilter(product_types=("footwear",), max_price=8000.0, price_currency="INR")
        out = HybridRetriever(conn=real_db).retrieve("shoe everyday sneakers", limit=10, filters=f)
        assert [p["id"] for p in out] == [3]  # USD 89.99 = ₹7,477
        assert all(p["category_name"] != "Electronics" and f.inr_price(p) <= 8000 for p in out)
        assert HybridRetriever(conn=real_db).retrieve(
            "shoe everyday sneakers", limit=10,
            filters=MetadataFilter(product_types=("footwear",), max_price=7000.0, price_currency="INR")) == []

    def test_relevance_floor_drops_hash_only_matches(self, real_db) -> None:
        from src.retrieval.hybrid import HybridRetriever

        assert HybridRetriever(conn=real_db).retrieve("coffee maker", limit=10) == []
        # The switch exists for when a real embedding model indexes the catalogue.
        assert HybridRetriever(conn=real_db, semantic_only_results=True).retrieve("coffee maker", limit=10)
