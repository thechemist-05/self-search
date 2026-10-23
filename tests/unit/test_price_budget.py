"""Price status and budget decisions (2026-10-23): every price is INR.

Verified INR source prices, USD source prices converted at the documented rate
(config/exchange_rates.toml: 83.0815 INR per USD, ECB 2023-09-29), estimates and unknown prices.
"""

from __future__ import annotations

import pytest

from src.pricing.budget import (
    ESTIMATE_LABEL,
    EstimateInfo,
    PriceView,
    budget_decision,
    classify_price,
    load_estimates,
    price_views,
)

EST = EstimateInfo(amount=2340.0, range_min=1500.0, range_max=6000.0, rule_key="sneakers", config_version="v1")


class TestClassify:
    def test_verified_needs_price_and_currency(self) -> None:
        assert classify_price(2999.0, "inr", None, estimates_enabled=False) == \
            PriceView("verified", 2999.0, "INR", original_currency="INR")

    def test_usd_is_converted_at_the_documented_rate(self) -> None:
        view = classify_price(49.99, "USD", None, estimates_enabled=False)
        assert (view.status, view.amount, view.currency, view.original_currency) == ("converted", 4153.0, "INR", "USD")
        assert view.fx.rate == 83.0815 and view.fx.rate_date == "2023-09-29" and "European Central Bank" in view.fx.source
        assert classify_price(1.0, "USD", None, estimates_enabled=False).amount == 83.0  # whole rupees, half up

    def test_source_price_always_wins_over_an_estimate(self) -> None:
        view = classify_price(49.99, "USD", EST, estimates_enabled=True)
        assert view.status == "converted" and view.amount == 4153.0 and view.estimate is None

    @pytest.mark.parametrize("price,currency,reason", [
        (10.0, "EUR", "no_documented_rate_for_EUR"),   # no rate in the config: never guessed
        (0.0, "USD", "invalid_source_price"),
        (-5.0, "INR", "invalid_source_price"),
        (0.004, "USD", "converts_to_zero"),            # never a misleading ₹0
    ])
    def test_unconvertible_or_invalid_prices_are_unavailable(self, price, currency, reason) -> None:
        view = classify_price(price, currency, EST, estimates_enabled=True)
        assert view.status == "unknown" and view.amount is None and view.reason == reason
        assert view.estimate is None  # a source price is never replaced by an estimate

    def test_estimate_only_when_enabled(self) -> None:
        assert classify_price(None, None, EST, estimates_enabled=False).status == "unknown"
        view = classify_price(None, None, EST, estimates_enabled=True)
        assert view == PriceView("estimated", 2340.0, "INR", EST)
        assert view.label == ESTIMATE_LABEL

    @pytest.mark.parametrize("price,currency", [(None, None), (None, "INR"), (1200.0, None), (1200.0, "")])
    def test_unknown(self, price, currency) -> None:
        view = classify_price(price, currency, None, estimates_enabled=True)
        assert view.status == "unknown" and view.amount is None and view.label is None

    def test_source_price_without_currency_is_not_replaced_by_estimate(self) -> None:
        assert classify_price(1200.0, None, EST, estimates_enabled=True).status == "unknown"


class TestBudget:
    INR_4000 = dict(budget_currency="INR", max_price=4000.0)

    def test_verified_inr(self) -> None:
        assert budget_decision(PriceView("verified", 2999.0, "INR"), **self.INR_4000) == \
            budget_decision(PriceView("verified", 2999.0, "INR"), budget_currency="inr", max_price=4000.0)
        d = budget_decision(PriceView("verified", 2999.0, "INR"), **self.INR_4000)
        assert d.matches and d.basis == "verified"
        assert not budget_decision(PriceView("verified", 4999.0, "INR"), **self.INR_4000).matches

    def test_estimated_inr_is_marked_as_estimated(self) -> None:
        d = budget_decision(PriceView("estimated", 2340.0, "INR", EST), **self.INR_4000)
        assert d.matches and d.basis == "estimated" and d.reason == "within_budget_estimated"

    def test_usd_source_prices_are_judged_on_their_inr_amount(self) -> None:
        cheap = classify_price(39.99, "USD", None, estimates_enabled=False)   # ₹3,322
        dear = classify_price(49.99, "USD", None, estimates_enabled=False)    # ₹4,153
        assert budget_decision(cheap, **self.INR_4000).matches
        assert budget_decision(cheap, **self.INR_4000).basis == "converted"
        # 49.99 is a small number, but ₹4,153 is over a ₹4,000 budget: never read as rupees.
        assert not budget_decision(dear, **self.INR_4000).matches

    def test_budgets_in_other_currencies_match_nothing(self) -> None:
        d = budget_decision(PriceView("estimated", 40.0, "INR", EST), budget_currency="USD", max_price=50.0)
        assert not d.matches and d.reason == "budget_not_in_inr"

    def test_unknown_never_satisfies(self) -> None:
        d = budget_decision(PriceView("unknown", None, None), **self.INR_4000)
        assert not d.matches and d.reason == "price_unknown"

    def test_unstated_budget_currency_matches_nothing(self) -> None:
        d = budget_decision(PriceView("verified", 10.0, "INR"), budget_currency=None, max_price=4000.0)
        assert not d.matches and d.reason == "budget_currency_unknown"

    def test_min_and_max(self) -> None:
        v = PriceView("verified", 1500.0, "INR")
        assert budget_decision(v, budget_currency="INR", min_price=1000.0, max_price=2000.0).matches
        assert not budget_decision(v, budget_currency="INR", min_price=1600.0).matches

    def test_no_budget_matches_everything(self) -> None:
        assert budget_decision(PriceView("unknown", None, None), budget_currency=None).reason == "no_budget"


class TestFromDatabase:
    def seed(self, conn) -> None:
        conn.execute("INSERT INTO data_sources (id, name) VALUES (1, 's')")
        for pid, price, cur in ((1, 2999.0, "INR"), (2, None, None), (3, 49.99, "USD"), (4, None, None)):
            conn.execute("INSERT INTO products (id, source_id, external_id, title, price, currency) "
                         "VALUES (?, 1, ?, 't', ?, ?)", (pid, f"e{pid}", price, cur))
        conn.execute("INSERT INTO price_estimates (product_id, currency, amount, range_min, range_max, rule_key, "
                     "method, config_version, config_sha256, label) VALUES "
                     "(2, 'INR', 2340, 1500, 6000, 'sneakers', 'm', 'v1', 'h', 'synthetic_estimate')")
        conn.commit()

    def test_four_states_and_budget(self, experimental_conn) -> None:
        self.seed(experimental_conn)
        views = price_views(experimental_conn, [1, 2, 3, 4], estimates_enabled=True)
        assert {k: v.status for k, v in views.items()} == {1: "verified", 2: "estimated", 3: "converted", 4: "unknown"}
        assert views[3].amount == 4153.0
        decisions = {k: budget_decision(v, budget_currency="INR", max_price=4000.0) for k, v in views.items()}
        assert {k: (d.matches, d.basis) for k, d in decisions.items()} == {
            1: (True, "verified"), 2: (True, "estimated"), 3: (False, "converted"), 4: (False, None)}
        assert budget_decision(views[3], budget_currency="INR", max_price=5000.0).matches

    def test_estimates_hidden_by_default(self, experimental_conn) -> None:
        self.seed(experimental_conn)
        views = price_views(experimental_conn, [2])  # default: estimates_enabled=False
        assert views[2].status == "unknown"
        assert not budget_decision(views[2], budget_currency="INR", max_price=4000.0).matches

    def test_database_without_estimates_table(self, catalogue_db) -> None:
        import sqlite3

        conn = sqlite3.connect(catalogue_db)
        assert load_estimates(conn, [1, 2]) == {}
        assert price_views(conn, [1], estimates_enabled=True)[1].status == "converted"  # a USD fixture price
        conn.close()


class TestRetrievalFilterBoundaries:
    """MetadataFilter.matches (what retrieval enforces) agrees with budget_decision at the edges."""

    @staticmethod
    def _filter(**kw):
        from src.retrieval.filters import MetadataFilter
        return MetadataFilter(price_currency="INR", use_price_estimates=True, **kw)

    @staticmethod
    def _product(price=None, currency=None, estimate=None):
        from src.retrieval.filters import ESTIMATE_KEY
        p = {"id": 1, "title": "Shoe", "price": price, "currency": currency}
        if estimate is not None:
            p[ESTIMATE_KEY] = estimate
        return p

    @pytest.mark.parametrize("estimate, expected", [(3000.0, True), (3000.01, False), (2999.99, True)])
    def test_max_is_inclusive_for_estimates(self, estimate, expected) -> None:
        assert self._filter(max_price=3000.0).matches(self._product(estimate=estimate)) is expected
        view = classify_price(None, None, EstimateInfo(estimate, 1, 9999, "k", "v"), estimates_enabled=True)
        assert budget_decision(view, budget_currency="INR", max_price=3000.0).matches is expected

    @pytest.mark.parametrize("estimate, expected", [(500.0, True), (499.99, False), (800.0, True), (800.01, False)])
    def test_range_bounds_are_inclusive(self, estimate, expected) -> None:
        assert self._filter(min_price=500.0, max_price=800.0).matches(self._product(estimate=estimate)) is expected

    def test_missing_price_is_never_zero(self) -> None:
        assert not self._filter(max_price=3000.0).matches(self._product())
        assert not self._filter(min_price=0.0).matches(self._product())

    def test_usd_price_is_compared_in_inr(self) -> None:
        # USD 20 -> ₹1,662 (under ₹3,000); USD 40 -> ₹3,323 (over). Never "20 rupees".
        assert self._filter(max_price=3000.0).matches(self._product(price=20.0, currency="USD"))
        assert not self._filter(max_price=3000.0).matches(self._product(price=40.0, currency="USD"))
        assert not self._filter(max_price=100.0).matches(self._product(price=20.0, currency="USD"))
        # ... and a source price wins over a stray estimate.
        assert not self._filter(max_price=1000.0).matches(self._product(price=20.0, currency="USD", estimate=900.0))

    def test_estimates_ignored_unless_enabled(self) -> None:
        from src.retrieval.filters import MetadataFilter
        off = MetadataFilter(price_currency="INR", max_price=3000.0, use_price_estimates=False)
        assert not off.matches(self._product(estimate=900.0))
