"""Documented exchange rates, INR formatting and foreign-price redaction (2026-10-23)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.pricing.fx import (
    DEFAULT_FX_CONFIG,
    FxConfigError,
    format_inr,
    get_fx_rates,
    inr_sql,
    load_fx_rates,
    to_inr,
)
from src.pricing.redact import REDACTED, contains_foreign_price, redact_foreign_prices


class TestRateConfig:
    def test_shipped_rate_is_the_ecb_cross_rate_with_provenance(self) -> None:
        usd = load_fx_rates(DEFAULT_FX_CONFIG)["USD"]
        assert usd.rate == 83.0815 == round(88.0165 / 1.0594, 4)
        assert usd.rate_date == "2023-09-29" and "European Central Bank" in usd.source
        assert usd.source_url.startswith("https://data-api.ecb.europa.eu/") and usd.version
        assert get_fx_rates()["USD"] == usd

    @pytest.mark.parametrize("body,error", [
        ('[USD]\nquote="INR"\nrate=83.0815\nrate_date="2023-09-29"\nsource="x"\nsource_url="u"\nretrieved="r"\n', "version"),
        ('version="v"\n[USD]\nquote="INR"\nrate_date="2023-09-29"\nsource="x"\nsource_url="u"\nretrieved="r"\n', "rate"),
        ('version="v"\n[USD]\nquote="EUR"\nrate=1\nrate_date="2023-09-29"\nsource="x"\nsource_url="u"\nretrieved="r"\n', "quote INR"),
        ('version="v"\n[USD]\nquote="INR"\nrate=90\nrate_date="2023-09-29"\nsource="x"\nsource_url="u"\nretrieved="r"\n'
         'ecb_inr_per_eur=88.0165\necb_usd_per_eur=1.0594\n', "cross rate"),
        ('version="v"\n[USD]\nquote="INR"\nrate=-1\nrate_date="2023-09-29"\nsource="x"\nsource_url="u"\nretrieved="r"\n', "plausible"),
    ])
    def test_invalid_rate_files_are_refused(self, tmp_path: Path, body: str, error: str) -> None:
        path = tmp_path / "fx.toml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(FxConfigError, match=error):
            load_fx_rates(path)

    def test_a_missing_rate_file_means_no_conversion(self, tmp_path: Path, monkeypatch) -> None:
        from src.config.settings import get_settings

        monkeypatch.setenv("SHELFSENSE_FX_RATE_CONFIG", str(tmp_path / "missing.toml"))
        get_settings.cache_clear()
        try:
            assert get_fx_rates() == {}
            assert to_inr(23.69, "USD") == (None, None)  # unavailable, never guessed
            assert to_inr(799.0, "INR")[0] == 799.0      # INR needs no rate
        finally:
            get_settings.cache_clear()


class TestConversion:
    @pytest.mark.parametrize("usd,inr", [(23.69, 1968.0), (7.99, 664.0), (49.99, 4153.0), (1.0, 83.0), (142.99, 11880.0)])
    def test_whole_rupees_half_up(self, usd, inr) -> None:
        amount, fx = to_inr(usd, "usd")
        assert amount == inr and fx.currency == "USD"

    def test_sql_expression_matches_python_exactly(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE products (price REAL, currency TEXT)")
        rows = [(23.69, "USD"), (7.99, "USD"), (0.006, "USD"), (1499.0, "INR"), (10.0, "EUR"), (5.0, None)]
        conn.executemany("INSERT INTO products VALUES (?, ?)", rows)
        expr, params = inr_sql("price", "currency", get_fx_rates())
        got = [r[0] for r in conn.execute(f"SELECT {expr} FROM products", params)]
        assert got == [to_inr(p, c)[0] if c else None for p, c in rows]


class TestFormatting:
    @pytest.mark.parametrize("amount,text", [(799, "₹799"), (1499, "₹1,499"), (12999, "₹12,999"), (125000, "₹1,25,000"),
                                             (10000000, "₹1,00,00,000"), (1499.5, "₹1,499.50"), (0, "₹0")])
    def test_indian_grouping(self, amount, text) -> None:
        assert format_inr(amount) == text


class TestRedaction:
    @pytest.mark.parametrize("text", ["costs $23.69", "about US$ 24", "USD 23.69 only", "23.69 USD", "24 dollars",
                                      "€10 off", "EUR 10", "£5", "5 pounds sterling", "$1,299.00 bundle"])
    def test_foreign_amounts_are_found_and_removed(self, text) -> None:
        assert contains_foreign_price(text)
        out = redact_foreign_prices(text)
        assert REDACTED in out and not contains_foreign_price(out)

    @pytest.mark.parametrize("text", ["costs ₹1,968", "Rs. 799 only", "INR 1,25,000", "a 6ft cable", "top 3 picks", ""])
    def test_rupees_and_plain_numbers_are_left_alone(self, text) -> None:
        assert not contains_foreign_price(text) and redact_foreign_prices(text) == text
