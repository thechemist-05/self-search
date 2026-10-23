"""Documented exchange rates and INR formatting (2026-10-23).

Self Search shows every product price in Indian rupees. A source price in another
currency is converted only with a rate from ``config/exchange_rates.toml`` (official
source, stated date, recorded provenance). A currency without a valid rate is never
converted, so its products have no INR price ("Price unavailable"). Nothing is guessed.

Converted amounts are rounded to whole rupees, half up: ``int(amount * rate + 0.5)``.
SQL filters use the identical expression (``inr_sql``), so a budget and a card always
agree on the same number.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger("shelfsense.pricing.fx")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FX_CONFIG = PROJECT_ROOT / "config" / "exchange_rates.toml"


class FxConfigError(ValueError):
    """The exchange-rate file is missing a field, inconsistent, or not from a stated source."""


@dataclass(frozen=True)
class FxRate:
    """One documented conversion rate into INR."""

    currency: str          # source currency, e.g. "USD"
    rate: float            # INR per one unit of ``currency``
    rate_date: str         # ISO date the rate applies to
    source: str
    source_url: str
    retrieved: str
    version: str

    def describe(self) -> str:
        """Short provenance text, e.g. "₹83.08 per US dollar (ECB reference rate, 2023-09-29)".

        Names the currency in words: no foreign currency symbol appears in user-facing text.
        """
        unit = {"USD": "US dollar", "EUR": "euro", "GBP": "pound sterling"}.get(self.currency, self.currency)
        where = "ECB reference rate, " if "European Central Bank" in self.source else ""
        return f"₹{self.rate:.2f} per {unit} ({where}{self.rate_date})"


def load_fx_rates(path: Path) -> dict[str, FxRate]:
    """Read and validate the rate file. Raises ``FxConfigError`` on any inconsistency."""
    raw = path.read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise FxConfigError(f"{path}: 'version' is required")
    rates: dict[str, FxRate] = {}
    for code, entry in data.items():
        if code == "version":
            continue
        if not isinstance(entry, dict) or not re.fullmatch(r"[A-Z]{3}", code):
            raise FxConfigError(f"{path}: [{code}] is not a currency table")
        for key in ("quote", "rate", "rate_date", "source", "source_url", "retrieved"):
            if key not in entry:
                raise FxConfigError(f"{path}: [{code}] is missing '{key}'")
        if entry["quote"] != "INR":
            raise FxConfigError(f"{path}: [{code}] must quote INR")
        rate = float(entry["rate"])
        if not 0 < rate < 10_000:
            raise FxConfigError(f"{path}: [{code}] rate {rate} is not plausible")
        date.fromisoformat(str(entry["rate_date"]))
        # A rate derived from the ECB's euro reference rates must equal their quotient.
        if "ecb_inr_per_eur" in entry or "ecb_usd_per_eur" in entry:
            cross = float(entry["ecb_inr_per_eur"]) / float(entry[f"ecb_{code.lower()}_per_eur"])
            if abs(round(cross, 4) - rate) > 0.00005:
                raise FxConfigError(f"{path}: [{code}] rate {rate} != ECB cross rate {cross:.4f}")
        rates[code] = FxRate(code, rate, str(entry["rate_date"]), entry["source"], entry["source_url"],
                             str(entry["retrieved"]), version)
    return rates


def fx_config_sha256(path: Path = DEFAULT_FX_CONFIG) -> Optional[str]:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


@lru_cache(maxsize=4)
def _cached(path_str: str) -> dict[str, FxRate]:
    path = Path(path_str)
    if not path.is_file():
        logger.warning("fx_config_missing", path=path_str, effect="non-INR source prices are unavailable")
        return {}
    try:
        return load_fx_rates(path)
    except (FxConfigError, ValueError, KeyError) as exc:
        logger.error("fx_config_invalid", path=path_str, error=str(exc),
                     effect="non-INR source prices are unavailable")
        return {}


def get_fx_rates() -> dict[str, FxRate]:
    """Rates from the configured file (``SHELFSENSE_FX_RATE_CONFIG``); empty if missing or invalid."""
    from src.config.settings import get_settings

    configured = Path(get_settings().fx_rate_config)
    path = configured if configured.is_absolute() else PROJECT_ROOT / configured
    return _cached(str(path))


def to_inr(amount: float, currency: Optional[str], rates: Optional[dict[str, FxRate]] = None
           ) -> tuple[Optional[float], Optional[FxRate]]:
    """(INR amount, rate used). INR passes through unchanged (rate None); a currency
    without a documented rate gives (None, None)."""
    code = (currency or "").upper()
    if code == "INR":
        return float(amount), None
    fx = (get_fx_rates() if rates is None else rates).get(code)
    if fx is None:
        return None, None
    return float(int(float(amount) * fx.rate + 0.5)), fx


def inr_sql(price_col: str, currency_col: str, rates: dict[str, FxRate]) -> tuple[str, list]:
    """SQL expression for a source price in INR, NULL when it cannot be converted.

    Mirrors ``to_inr`` exactly (INR unchanged; documented rates, whole rupees, half up).
    """
    parts = [f"WHEN UPPER({currency_col}) = 'INR' THEN {price_col}"]
    params: list = []
    for code, fx in sorted(rates.items()):
        parts.append(f"WHEN UPPER({currency_col}) = ? THEN CAST({price_col} * ? + 0.5 AS INTEGER)")
        params += [code, fx.rate]
    return f"(CASE {' '.join(parts)} ELSE NULL END)", params


def format_inr(amount: float) -> str:
    """Indian digit grouping: 799 -> "₹799", 1499 -> "₹1,499", 125000 -> "₹1,25,000".

    Whole rupees unless the amount has paise ("₹1,499.50").
    """
    amount = float(amount)
    rupees = int(abs(amount))
    paise = round((abs(amount) - rupees) * 100)
    if paise == 100:
        rupees, paise = rupees + 1, 0
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    sign = "-" if amount < 0 else ""
    return f"{sign}₹{digits}" + (f".{paise:02d}" if paise else "")
