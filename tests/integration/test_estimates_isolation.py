"""Synthetic estimates never reach prompts, fallback answers, evidence packs or verified claims.

Runs the existing retrieval, prompt, fallback, evidence and validator code unchanged
against an experimental database that holds estimates.
"""

from __future__ import annotations

import json
import re

import pytest

from src.config.settings import PROJECT_ROOT
from src.data.adapters.amazon_reviews_2023 import AmazonReviews2023Adapter
from src.data.adapters.pipeline import ingest, read_jsonl
from src.evidence.evidence_pack import assemble_evidence_pack
from src.evidence.numeric import NumericVerifier
from src.llm.prompts import build_chat_messages
from src.llm.provider import MockLLMProvider
from src.llm.service import LLMService
from src.pricing.estimates import generate_estimates, load_range_config
from src.retrieval.keyword import keyword_search_products

FIX = PROJECT_ROOT / "tests" / "fixtures" / "adapters"


@pytest.fixture
def estimated(experimental_conn):
    """Experimental DB with the Amazon fixture ingested and estimates generated."""
    ingest(experimental_conn, AmazonReviews2023Adapter(revision="fixture"),
           read_jsonl(FIX / "amazon2023_meta.jsonl"), read_jsonl(FIX / "amazon2023_reviews.jsonl"))
    generate_estimates(experimental_conn, load_range_config(PROJECT_ROOT / "config" / "price_estimate_ranges.toml"))
    rows = experimental_conn.execute(
        "SELECT p.id, p.title, e.amount FROM price_estimates e JOIN products p ON p.id = e.product_id").fetchall()
    assert len(rows) >= 2, "fixture should produce estimates"
    return experimental_conn, {pid: (title, amount) for pid, title, amount in rows}


def amount_patterns(amount: float) -> list[str]:
    n = int(amount)
    return [f"{n:,}", f"{amount:.2f}", f"{amount:,.2f}", rf"\b{n}\b"]


def assert_absent(text: str, estimates: dict) -> None:
    for _, amount in estimates.values():
        for pat in amount_patterns(amount):
            assert not re.search(pat, text), f"estimate {amount} leaked: {pat!r}"
    assert "INR" not in text and "synthetic" not in text.lower()


def candidates(conn, estimates):
    products = []
    for title, _ in estimates.values():
        products += keyword_search_products(conn, title.split()[0], limit=10)
    assert {p["id"] for p in products} >= set(estimates)
    return products


def test_retrieved_products_carry_only_source_prices(estimated) -> None:
    conn, estimates = estimated
    for p in candidates(conn, estimates):
        if p["id"] in estimates:
            assert p.get("price") is None and p.get("currency") is None
        assert "estimate" not in json.dumps(p, default=str).lower()


def test_prompt_never_contains_estimates(estimated) -> None:
    conn, estimates = estimated
    products = candidates(conn, estimates)
    pack = assemble_evidence_pack(conn, [p["id"] for p in products], query="sneakers")
    text = json.dumps(build_chat_messages("sneakers under 4000 rupees", products, pack), ensure_ascii=False)
    assert_absent(text, estimates)
    for pid in estimates:
        assert f"Product ID: {pid} " in text  # the product is there, with its price shown as unavailable
    assert "Price: unavailable" in text


def test_evidence_pack_never_contains_estimates(estimated) -> None:
    conn, estimates = estimated
    pack = assemble_evidence_pack(conn, list(estimates), query="")
    assert_absent(json.dumps(pack.to_dict(), ensure_ascii=False) + pack.format_for_prompt(), estimates)


def test_fallback_answer_never_contains_estimates(estimated) -> None:
    conn, estimates = estimated
    products = candidates(conn, estimates)
    service = LLMService(provider=MockLLMProvider(available=False))
    answer = service.build_safe_fallback("sneakers", products, assemble_evidence_pack(conn, list(estimates)))
    assert_absent(answer.model_dump_json(), estimates)


@pytest.mark.parametrize("template", ["It costs ${n:.2f}.", "Priced at {n:.0f} USD.", "Only ₹{n:,.0f}!",
                                      "Costs Rs {n:.0f}."])
def test_estimate_quoting_claim_is_never_verified(estimated, template: str) -> None:
    conn, estimates = estimated
    verifier = NumericVerifier()
    for pid, (_, amount) in estimates.items():
        results = verifier.verify_claims(conn, pid, template.format(n=amount))
        assert all(r.status != "VERIFIED_MATCH" for r in results)
        assert all(r.database_value is None for r in results)
