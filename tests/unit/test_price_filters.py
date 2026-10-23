"""MetadataFilter on normalised INR prices (2026-10-23): SQL and in-memory paths agree.

USD source prices are compared on their documented INR conversion (USD 49.99 -> ₹4,153);
estimates apply only to unpriced products; unknown prices and non-INR budgets never match.
"""

from __future__ import annotations

import pytest

from src.retrieval.filters import ESTIMATE_KEY, MetadataFilter, strip_estimates

# id: (title, price, currency, INR estimate)
ROWS = {
    1: ("Verified INR sneakers", 2999.0, "INR", None),
    2: ("Verified USD sneakers", 49.99, "USD", None),
    3: ("Estimated sneakers", None, None, 2340.0),
    4: ("Unknown-price sneakers", None, None, None),
    5: ("Expensive estimated sneakers", None, None, 5800.0),
    6: ("Source price without currency", 1200.0, None, None),
}


@pytest.fixture
def conn(experimental_conn):
    c = experimental_conn
    c.execute("INSERT INTO data_sources (id, name) VALUES (1, 's')")
    for pid, (title, price, cur, est) in ROWS.items():
        c.execute("INSERT INTO products (id, source_id, external_id, title, price, currency) VALUES (?, 1, ?, ?, ?, ?)",
                  (pid, f"e{pid}", title, price, cur))
        if est is not None:
            c.execute("INSERT INTO price_estimates (product_id, currency, amount, range_min, range_max, rule_key, "
                      "method, config_version, config_sha256, label) VALUES (?, 'INR', ?, 1500, 6000, 'sneakers', "
                      "'m', 'v1', 'h', 'synthetic_estimate')", (pid, est))
    c.commit()
    return c


def sql_ids(conn, f: MetadataFilter) -> set[int]:
    where, params = f.build_sql_conditions("p")
    return {r[0] for r in conn.execute(f"SELECT p.id FROM products p WHERE {where}", params)}


def py_ids(conn, f: MetadataFilter) -> set[int]:
    col = f.estimate_column("p")
    rows = conn.execute(f"SELECT p.id, p.price, p.currency{', ' + col if col else ''} FROM products p").fetchall()
    out = set()
    for r in rows:
        product = {"id": r[0], "price": r[1], "currency": r[2]}
        if col:
            product[ESTIMATE_KEY] = r[3]
        if f.matches(product):
            out.add(r[0])
    return out


def test_disabled_flag_keeps_old_behaviour(conn) -> None:
    f = MetadataFilter(max_price=4000.0, price_currency="INR")
    assert f.use_price_estimates is False and f.estimate_column() is None
    assert sql_ids(conn, f) == py_ids(conn, f) == {1}


def test_enabled_inr_budget_uses_estimates_only_without_source_price(conn) -> None:
    f = MetadataFilter(max_price=4000.0, price_currency="INR", use_price_estimates=True)
    assert sql_ids(conn, f) == py_ids(conn, f) == {1, 3}  # verified INR + estimate; not USD, unknown, >4000


def test_min_and_max_with_estimates(conn) -> None:
    f = MetadataFilter(min_price=2500.0, max_price=6000.0, price_currency="INR", use_price_estimates=True)
    assert sql_ids(conn, f) == py_ids(conn, f) == {1, 2, 5}  # 2 is USD 49.99 = ₹4,153


def test_converted_usd_price_uses_its_inr_amount(conn) -> None:
    under = MetadataFilter(max_price=4153.0, price_currency="INR")
    over = MetadataFilter(max_price=4152.0, price_currency="INR")
    assert 2 in sql_ids(conn, under) and 2 in py_ids(conn, under)
    assert 2 not in sql_ids(conn, over) and 2 not in py_ids(conn, over)
    assert 2 not in sql_ids(conn, MetadataFilter(max_price=100.0, price_currency="INR"))  # never "49.99 rupees"


def test_non_inr_budgets_match_nothing(conn) -> None:
    f = MetadataFilter(max_price=100000.0, price_currency="USD", use_price_estimates=True)
    assert f.estimate_column() is None
    assert sql_ids(conn, f) == py_ids(conn, f) == set()


def test_unstated_budget_currency_is_inr(conn) -> None:
    f = MetadataFilter(max_price=4000.0, use_price_estimates=True)
    assert sql_ids(conn, f) == py_ids(conn, f) == {1, 3}


def test_unknown_prices_never_satisfy_a_budget(conn) -> None:
    f = MetadataFilter(max_price=1e9, price_currency="INR", use_price_estimates=True)
    ids = sql_ids(conn, f)
    assert ids == py_ids(conn, f) == {1, 2, 3, 5}
    assert not {4, 6} & ids  # unknown, source price without currency


def test_source_price_wins_over_a_stray_estimate(conn) -> None:
    conn.execute("INSERT INTO price_estimates (product_id, currency, amount, range_min, range_max, rule_key, method, "
                 "config_version, config_sha256, label) VALUES (2, 'INR', 100, 50, 200, 'x', 'm', 'v', 'h', "
                 "'synthetic_estimate')")  # should never exist; the source USD price must still win
    f = MetadataFilter(max_price=4000.0, price_currency="INR", use_price_estimates=True)
    assert 2 not in sql_ids(conn, f) and 2 not in py_ids(conn, f)


def test_no_price_bound_ignores_flag(conn) -> None:
    f = MetadataFilter(use_price_estimates=True)
    assert f.estimate_column() is None and f.build_sql_conditions("p") == ("", [])


def test_strip_estimates() -> None:
    products = [{"id": 1, ESTIMATE_KEY: 2340.0}, {"id": 2}]
    assert strip_estimates(products) == [{"id": 1}, {"id": 2}]


def test_retrievers_never_return_the_estimate_key(conn) -> None:
    from src.retrieval.keyword import keyword_search_products

    f = MetadataFilter(max_price=4000.0, price_currency="INR", use_price_estimates=True,
                       product_types=("sneakers",))
    results = keyword_search_products(conn, "sneakers", limit=10, filters=f)
    assert {r["id"] for r in results} == {1, 3}
    assert all(ESTIMATE_KEY not in r for r in results)
