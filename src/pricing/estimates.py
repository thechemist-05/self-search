"""Deterministic synthetic INR price estimates (experimental, off by default).

An estimate is a *demonstration value*, not a price: a point inside an illustrative,
hand-written category range (``config/price_estimate_ranges.toml``), chosen from
``sha256(config version | source | external id)``. It never uses a source price, so
no USD value is ever converted to INR.

Estimates live only in the experimental ``price_estimates`` table. This module never
writes to ``products`` and only estimates products whose source supplied no price.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.db.experimental_schema import has_price_estimates

LABEL = "synthetic_estimate"


class PriceEstimateConfigError(ValueError):
    """The ranges file is missing required fields or contains invalid ranges."""


@dataclass(frozen=True)
class RangeRule:
    """One illustrative range, matched by keywords in the title or category path."""

    key: str
    min: float
    max: float
    title_keywords: tuple[str, ...] = ()
    category_keywords: tuple[str, ...] = ()

    def matches(self, title: str, category_path: str) -> bool:
        return any(_has_phrase(title, k) for k in self.title_keywords) or any(
            _has_phrase(category_path, k) for k in self.category_keywords
        )


@dataclass(frozen=True)
class RangeConfig:
    """A versioned set of illustrative INR ranges."""

    version: str
    method: str
    disclaimer: str
    sha256: str
    rules: tuple[RangeRule, ...]
    currency: str = "INR"

    def rule_for(self, title: str, category_path: str = "") -> Optional[RangeRule]:
        """First rule, in file order, whose keywords match."""
        return next((r for r in self.rules if r.matches(title or "", category_path or "")), None)


@dataclass
class EstimateReport:
    """Counts from one ``generate_estimates`` run."""

    config_version: str
    estimated: int = 0
    unchanged: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def _has_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase.casefold())}(?![a-z0-9])", text.casefold()) is not None


def load_range_config(path: Path) -> RangeConfig:
    """Load and validate the ranges file. Raises ``PriceEstimateConfigError``."""
    raw = Path(path).read_bytes()
    try:
        data: dict[str, Any] = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise PriceEstimateConfigError(f"unreadable ranges file: {exc}") from exc
    for key in ("version", "currency", "method", "disclaimer"):
        if not str(data.get(key) or "").strip():
            raise PriceEstimateConfigError(f"missing '{key}'")
    if data["currency"] != "INR":
        raise PriceEstimateConfigError(f"estimates are INR only, got {data['currency']!r}")
    rules, seen = [], set()
    for i, r in enumerate(data.get("rule") or []):
        key = str(r.get("key") or "").strip()
        lo, hi = r.get("min"), r.get("max")
        if not key or key in seen:
            raise PriceEstimateConfigError(f"rule {i}: missing or duplicate key {key!r}")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lo, hi)) or not 0 < lo < hi:
            raise PriceEstimateConfigError(f"rule {key!r}: need 0 < min < max, got {lo!r}..{hi!r}")
        if not (r.get("title_keywords") or r.get("category_keywords")):
            raise PriceEstimateConfigError(f"rule {key!r}: no keywords")
        seen.add(key)
        rules.append(RangeRule(key, float(lo), float(hi), tuple(r.get("title_keywords") or ()),
                               tuple(r.get("category_keywords") or ())))
    if not rules:
        raise PriceEstimateConfigError("no rules")
    return RangeConfig(version=data["version"], method=data["method"], disclaimer=data["disclaimer"],
                       sha256=hashlib.sha256(raw).hexdigest(), rules=tuple(rules))


def estimate_amount(config: RangeConfig, rule: RangeRule, source_name: str, external_id: str) -> float:
    """Deterministic point in ``[rule.min, rule.max]`` for one product, rounded to 10."""
    digest = hashlib.sha256(f"{config.version}|{source_name}|{external_id}".encode("utf-8")).digest()
    u = int.from_bytes(digest[:8], "big") / 2**64
    amount = round((rule.min + u * (rule.max - rule.min)) / 10) * 10
    return float(min(max(amount, rule.min), rule.max))


def _category_text(category: Optional[str], attributes_json: Optional[str]) -> str:
    """Category name plus the full source path (adapters keep it in attributes_json)."""
    parts = [category or ""]
    try:
        path = (json.loads(attributes_json) or {}).get("category_path") if attributes_json else None
    except ValueError:
        path = None
    if isinstance(path, list):
        parts.extend(str(p) for p in path)
    return " > ".join(p for p in parts if p)


def generate_estimates(conn: sqlite3.Connection, config: RangeConfig) -> EstimateReport:
    """Write estimates for every product without a verified source price.

    A product is skipped when its source supplied a price (with or without a
    currency) or when no rule matches. Re-running with the same config leaves rows
    unchanged; a new config version replaces them. ``products`` is never written.
    """
    if not has_price_estimates(conn):
        raise RuntimeError("price_estimates table missing: apply src.db.experimental_schema first")
    report = EstimateReport(config_version=config.version)
    rows = conn.execute(
        "SELECT p.id, p.external_id, p.title, c.name, p.attributes_json, p.price, p.currency, s.name "
        "FROM products p JOIN data_sources s ON s.id = p.source_id "
        "LEFT JOIN categories c ON c.id = p.category_id ORDER BY p.id"
    ).fetchall()
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN")
    try:
        for pid, ext_id, title, category, attrs, price, currency, source in rows:
            if price is not None:
                report.skip("verified_price_present" if currency else "source_price_without_currency")
                conn.execute("DELETE FROM price_estimates WHERE product_id = ?", (pid,))
                continue
            rule = config.rule_for(title or "", _category_text(category, attrs))
            if rule is None:
                report.skip("no_matching_range")
                conn.execute("DELETE FROM price_estimates WHERE product_id = ?", (pid,))
                continue
            amount = estimate_amount(config, rule, source, ext_id)
            existing = conn.execute(
                "SELECT amount, rule_key, config_version, config_sha256 FROM price_estimates WHERE product_id = ?",
                (pid,),
            ).fetchone()
            if existing is not None and tuple(existing) == (amount, rule.key, config.version, config.sha256):
                report.unchanged += 1
                continue
            conn.execute(
                "INSERT OR REPLACE INTO price_estimates (product_id, currency, amount, range_min, range_max, "
                "rule_key, method, config_version, config_sha256, label) VALUES (?, 'INR', ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, amount, rule.min, rule.max, rule.key, config.method, config.version, config.sha256, LABEL),
            )
            report.estimated += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return report
