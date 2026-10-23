"""Price status and budget decisions. Every price that is shown or filtered is in INR.

* **verified**: the source supplied a price in INR (``products.price``/``currency``).
* **converted**: the source supplied a price in another currency, converted to INR with
  a documented rate (``src/pricing/fx.py``, ``config/exchange_rates.toml``): official
  source, stated date, whole rupees. The original amount stays in the database only.
* **estimated**: no source price, a synthetic INR estimate exists, and estimates are
  explicitly enabled. Always labelled as synthetic, never a retailer price.
* **unknown**: everything else: no price, a currency without a documented rate, a
  price without a currency, or a non-positive price ("Price unavailable").

A budget is in INR and is met by the normalised INR amount of a verified, converted or
(when enabled) estimated price. Unknown prices never satisfy a budget. Bounds are inclusive.

Retrieval applies the same rules in ``MetadataFilter`` (SQL and Python); this module
classifies prices for the API cards (``price_status``, ``estimated_price``,
``price_conversion``) and the requirement checks.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable, Literal, Optional

from src.db.experimental_schema import has_price_estimates
from src.pricing.fx import FxRate, to_inr

PriceStatus = Literal["verified", "converted", "estimated", "unknown"]
ESTIMATE_LABEL = "Synthetic estimate, not a retailer price"


@dataclass(frozen=True)
class EstimateInfo:
    """A stored synthetic estimate (always INR)."""

    amount: float
    range_min: float
    range_max: float
    rule_key: str
    config_version: str
    currency: str = "INR"


@dataclass(frozen=True)
class PriceView:
    """How a product's price may be shown and used. ``amount`` is always INR (or None)."""

    status: PriceStatus
    amount: Optional[float]
    currency: Optional[str]          # "INR" whenever ``amount`` is set
    estimate: Optional[EstimateInfo] = None
    original_currency: Optional[str] = None   # source currency of a verified/converted price
    fx: Optional[FxRate] = None               # rate used for a converted price
    reason: Optional[str] = None              # why an unknown price is unknown

    @property
    def label(self) -> Optional[str]:
        return ESTIMATE_LABEL if self.status == "estimated" else None


@dataclass(frozen=True)
class BudgetDecision:
    """Whether a product meets a budget, and on what basis."""

    matches: bool
    basis: Optional[Literal["verified", "converted", "estimated"]]
    reason: str


def classify_price(
    price: Optional[float],
    currency: Optional[str],
    estimate: Optional[EstimateInfo],
    *,
    estimates_enabled: bool,
    rates: Optional[dict[str, FxRate]] = None,
) -> PriceView:
    """Classify one product's price into INR. A source price always wins over an estimate.

    A source price that cannot be converted (no documented rate) is *unknown*; it is
    never replaced by an estimate, because estimates are only for products the source
    did not price.
    """
    if price is not None:
        code = (currency or "").upper()
        if not code:
            return PriceView("unknown", None, None, reason="source_price_without_currency")
        if float(price) <= 0:
            return PriceView("unknown", None, None, original_currency=code, reason="invalid_source_price")
        amount, fx = to_inr(float(price), code, rates)
        if amount is None:
            return PriceView("unknown", None, None, original_currency=code, reason=f"no_documented_rate_for_{code}")
        if amount <= 0:  # rounds to ₹0: a misleading zero, not a price
            return PriceView("unknown", None, None, original_currency=code, reason="converts_to_zero")
        if code == "INR":
            return PriceView("verified", amount, "INR", original_currency="INR")
        return PriceView("converted", amount, "INR", original_currency=code, fx=fx)
    if estimate is not None and estimates_enabled:
        return PriceView("estimated", estimate.amount, estimate.currency, estimate)
    return PriceView("unknown", None, None, reason="no_price")


def budget_decision(
    view: PriceView,
    *,
    budget_currency: Optional[str],
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
) -> BudgetDecision:
    """Decide whether ``view`` meets an INR budget, on its normalised INR amount."""
    if min_price is None and max_price is None:
        return BudgetDecision(True, None, "no_budget")
    if not budget_currency:
        return BudgetDecision(False, None, "budget_currency_unknown")
    if budget_currency.upper() != "INR":
        return BudgetDecision(False, None, "budget_not_in_inr")
    if view.status == "unknown" or view.amount is None:
        return BudgetDecision(False, None, "price_unknown")
    if (min_price is not None and view.amount < min_price) or (max_price is not None and view.amount > max_price):
        return BudgetDecision(False, view.status, "outside_budget")
    return BudgetDecision(True, view.status, f"within_budget_{view.status}")


def load_estimates(conn: sqlite3.Connection, product_ids: Iterable[int]) -> dict[int, EstimateInfo]:
    """Estimates for these products; empty when the database has no ``price_estimates`` table."""
    ids = list(product_ids)
    if not ids or not has_price_estimates(conn):
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT product_id, amount, range_min, range_max, rule_key, config_version, currency "
        f"FROM price_estimates WHERE product_id IN ({marks})", ids,
    ).fetchall()
    return {r[0]: EstimateInfo(r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows}


def price_views(
    conn: sqlite3.Connection, product_ids: Iterable[int], *, estimates_enabled: bool = False
) -> dict[int, PriceView]:
    """Price view per product. ``estimates_enabled`` defaults to False (estimates hidden)."""
    ids = list(product_ids)
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT id, price, currency FROM products WHERE id IN ({marks})", ids).fetchall()
    estimates = load_estimates(conn, ids) if estimates_enabled else {}
    return {pid: classify_price(price, cur, estimates.get(pid), estimates_enabled=estimates_enabled)
            for pid, price, cur in rows}
