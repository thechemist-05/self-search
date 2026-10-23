"""Synthetic INR estimates: config validation, determinism, versioning, never touching source prices."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.config.settings import PROJECT_ROOT, get_settings
from src.pricing import estimates as est

CONFIG = PROJECT_ROOT / "config" / "price_estimate_ranges.toml"

MINI = """
version = "{version}"
currency = "{currency}"
method = "category_range_uniform_v1"
disclaimer = "Illustrative assumption, not a market price."
[[rule]]
key = "sneakers"
title_keywords = ["sneaker", "sneakers"]
category_keywords = []
min = 1500
max = 6000
[[rule]]
key = "backpacks"
title_keywords = ["backpack"]
category_keywords = ["backpacks"]
min = 600
max = 3500
"""


def write_config(tmp_path: Path, version: str = "test-v1", currency: str = "INR", body: str = MINI) -> Path:
    path = tmp_path / f"ranges-{version}.toml"
    path.write_text(body.format(version=version, currency=currency), encoding="utf-8")
    return path


def seed(conn: sqlite3.Connection) -> None:
    """Products covering every price situation (ids 1-6)."""
    conn.execute("INSERT INTO data_sources (id, name) VALUES (1, 'src-a')")
    conn.execute("INSERT INTO categories (id, name) VALUES (1, 'Fashion')")
    rows = [
        (1, "A-1", "Street Sneakers", 49.99, "USD", None),          # verified USD
        (2, "A-2", "Canvas Sneakers", None, None, None),             # estimate (title)
        (3, "A-3", "Daypack 20L", None, None, ["Fashion", "Backpacks"]),  # estimate (category path)
        (4, "A-4", "Ceramic Vase", None, None, None),                # no matching range
        (5, "A-5", "Trail Sneakers", 1200.0, None, None),            # price without currency
        (6, "A-6", "Budget Sneakers", 2999.0, "INR", None),          # verified INR
    ]
    for pid, ext, title, price, cur, path in rows:
        attrs = json.dumps({"category_path": path}) if path else None
        conn.execute("INSERT INTO products (id, source_id, external_id, title, category_id, price, currency, "
                     "attributes_json) VALUES (?, 1, ?, ?, 1, ?, ?, ?)", (pid, ext, title, price, cur, attrs))
    conn.commit()


def products_snapshot(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute("SELECT * FROM products ORDER BY id").fetchall()


class TestConfig:
    def test_project_config_is_valid_and_labelled(self) -> None:
        cfg = est.load_range_config(CONFIG)
        assert cfg.currency == "INR" and cfg.version and len(cfg.rules) >= 5
        text = CONFIG.read_text(encoding="utf-8")
        assert "ILLUSTRATIVE ASSUMPTIONS, NOT MARKET DATA" in text
        assert "not a market or retailer price" in cfg.disclaimer

    def test_settings_point_to_the_config_and_default_off(self) -> None:
        get_settings.cache_clear()
        s = get_settings()
        assert s.estimated_prices_enabled is False
        assert (PROJECT_ROOT / s.price_estimate_config) == CONFIG

    @pytest.mark.parametrize("body,match", [
        (MINI.replace('version = "{version}"', ""), "missing 'version'"),
        (MINI.replace("disclaimer", "x_disclaimer"), "missing 'disclaimer'"),
        (MINI.replace("min = 600", "min = 4000"), "need 0 < min < max"),
        (MINI.replace('key = "backpacks"', 'key = "sneakers"'), "duplicate key"),
        (MINI.replace('title_keywords = ["backpack"]\ncategory_keywords = ["backpacks"]',
                      "title_keywords = []\ncategory_keywords = []"), "no keywords"),
        ("not = [valid", "unreadable"),
    ])
    def test_invalid_configs_rejected(self, tmp_path: Path, body: str, match: str) -> None:
        with pytest.raises(est.PriceEstimateConfigError, match=match):
            est.load_range_config(write_config(tmp_path, body=body))

    def test_non_inr_currency_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(est.PriceEstimateConfigError, match="INR only"):
            est.load_range_config(write_config(tmp_path, currency="USD"))

    def test_rules_match_whole_words_in_file_order(self, tmp_path: Path) -> None:
        cfg = est.load_range_config(write_config(tmp_path))
        assert cfg.rule_for("Canvas Sneakers").key == "sneakers"
        assert cfg.rule_for("Laptop Backpack with sneaker pocket").key == "sneakers"  # first rule wins
        assert cfg.rule_for("Sneakerhead poster") is None  # whole words only
        assert cfg.rule_for("Daypack", "Fashion > Backpacks").key == "backpacks"


class TestAmount:
    def test_deterministic_and_inside_range(self, tmp_path: Path) -> None:
        cfg = est.load_range_config(write_config(tmp_path))
        rule = cfg.rules[0]
        values = {est.estimate_amount(cfg, rule, "src-a", f"ID-{i}") for i in range(200)}
        assert all(rule.min <= v <= rule.max and v % 10 == 0 for v in values)
        assert len(values) > 50  # spread across the range
        assert est.estimate_amount(cfg, rule, "src-a", "ID-1") == est.estimate_amount(cfg, rule, "src-a", "ID-1")

    def test_depends_on_version_source_and_id(self, tmp_path: Path) -> None:
        v1 = est.load_range_config(write_config(tmp_path, "test-v1"))
        v2 = est.load_range_config(write_config(tmp_path, "test-v2"))
        r = v1.rules[0]
        base = est.estimate_amount(v1, r, "src-a", "X")
        others = [est.estimate_amount(v2, r, "src-a", "X"), est.estimate_amount(v1, r, "src-b", "X"),
                  est.estimate_amount(v1, r, "src-a", "Y")]
        assert any(o != base for o in others)
        assert est.estimate_amount(v2, r, "src-a", "X") != base


class TestGenerate:
    def test_estimates_only_unpriced_matching_products(self, experimental_conn, tmp_path: Path) -> None:
        seed(experimental_conn)
        cfg = est.load_range_config(write_config(tmp_path))
        report = est.generate_estimates(experimental_conn, cfg)
        rows = dict(experimental_conn.execute("SELECT product_id, rule_key FROM price_estimates").fetchall())
        assert rows == {2: "sneakers", 3: "backpacks"}
        assert report.estimated == 2
        assert report.skipped == {"verified_price_present": 2, "no_matching_range": 1,
                                  "source_price_without_currency": 1}

    def test_never_modifies_products(self, experimental_conn, tmp_path: Path) -> None:
        seed(experimental_conn)
        before = products_snapshot(experimental_conn)
        est.generate_estimates(experimental_conn, est.load_range_config(write_config(tmp_path)))
        assert products_snapshot(experimental_conn) == before

    def test_stored_rows_are_labelled_and_traceable(self, experimental_conn, tmp_path: Path) -> None:
        seed(experimental_conn)
        cfg = est.load_range_config(write_config(tmp_path))
        est.generate_estimates(experimental_conn, cfg)
        row = experimental_conn.execute(
            "SELECT currency, label, method, config_version, config_sha256, range_min, range_max, amount "
            "FROM price_estimates WHERE product_id = 2").fetchone()
        assert row[:5] == ("INR", "synthetic_estimate", "category_range_uniform_v1", "test-v1", cfg.sha256)
        assert row[5] <= row[7] <= row[6]
        assert row[7] == est.estimate_amount(cfg, cfg.rules[0], "src-a", "A-2")

    def test_rerun_same_config_is_unchanged(self, experimental_conn, tmp_path: Path) -> None:
        seed(experimental_conn)
        cfg = est.load_range_config(write_config(tmp_path))
        est.generate_estimates(experimental_conn, cfg)
        first = experimental_conn.execute("SELECT * FROM price_estimates ORDER BY product_id").fetchall()
        report = est.generate_estimates(experimental_conn, cfg)
        assert report.estimated == 0 and report.unchanged == 2
        assert experimental_conn.execute("SELECT * FROM price_estimates ORDER BY product_id").fetchall() == first

    def test_new_version_replaces_estimates(self, experimental_conn, tmp_path: Path) -> None:
        seed(experimental_conn)
        est.generate_estimates(experimental_conn, est.load_range_config(write_config(tmp_path, "test-v1")))
        est.generate_estimates(experimental_conn, est.load_range_config(write_config(tmp_path, "test-v2")))
        versions = {r[0] for r in experimental_conn.execute("SELECT config_version FROM price_estimates")}
        assert versions == {"test-v2"}

    def test_estimate_removed_once_a_source_price_appears(self, experimental_conn, tmp_path: Path) -> None:
        seed(experimental_conn)
        cfg = est.load_range_config(write_config(tmp_path))
        est.generate_estimates(experimental_conn, cfg)
        experimental_conn.execute("UPDATE products SET price = 30, currency = 'USD' WHERE id = 2")
        experimental_conn.commit()
        est.generate_estimates(experimental_conn, cfg)
        assert experimental_conn.execute("SELECT 1 FROM price_estimates WHERE product_id = 2").fetchone() is None

    def test_requires_experimental_table(self, catalogue_db: Path, tmp_path: Path) -> None:
        conn = sqlite3.connect(catalogue_db)
        with pytest.raises(RuntimeError, match="price_estimates table missing"):
            est.generate_estimates(conn, est.load_range_config(write_config(tmp_path)))
        conn.close()
