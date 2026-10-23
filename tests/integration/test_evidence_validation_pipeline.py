"""
Integration tests for the Phase 6 Evidence Validation Pipeline in Self Search.

Tests end-to-end integration across:
- SQLite product and review storage
- Evidence pack assembly
- LLM response generation (via deterministic offline MockLLMProvider)
- Evidence validation (citation checks, numeric recomputation, similarity grounding)
- SupportReport generation in both 'flag' and 'filter' modes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
import pytest

from src.evidence.evidence_pack import assemble_evidence_pack
from src.evidence.report import ValidationStatus
from src.evidence.validator import EvidenceValidator
from src.llm.provider import MockLLMProvider
from src.llm.schemas import EvidenceClaim, ProductRecommendation, StructuredAnswer
from src.llm.service import LLMService


@pytest.fixture
def db_conn() -> sqlite3.Connection:
    """Connect to the processed SQLite database."""
    db_path = Path("data/processed/shelfsense.db")
    if not db_path.exists():
        pytest.skip("Processed database data/processed/shelfsense.db not found.")
    conn = sqlite3.connect(str(db_path))
    yield conn
    conn.close()


def test_end_to_end_validation_faithful_answer(db_conn: sqlite3.Connection) -> None:
    """End-to-end test with a faithful, ground-truth-aligned LLM answer."""
    # 1. Fetch real product from DB
    row = db_conn.execute(
        "SELECT id, external_id, title, description, price, avg_rating, rating_count FROM products WHERE external_id = 'P001'"
    ).fetchone()
    assert row is not None
    p_id, ext_id, title, desc, price, rating, count = row

    product_dict = {
        "id": p_id,
        "external_id": ext_id,
        "title": title,
        "description": desc,
        "price": price,
        "avg_rating": rating,
        "rating_count": count,
    }

    # 2. Assemble evidence pack for Product 1
    evidence_pack = assemble_evidence_pack(
        conn=db_conn,
        product_ids=[p_id],
        query="waterproof lightweight backpack",
        token_budget=1500,
    )
    assert len(evidence_pack.items) > 0
    # Find snippet discussing waterproofing
    waterproof_eid = next(
        it.evidence_id for it in evidence_pack.items if "waterproof" in it.text.lower()
    )

    # 3. Simulate faithful LLM response referencing the real citation ID and the correct price, in
    # INR (the only currency prices are shown in; the source price is converted at the documented rate)
    from src.pricing.budget import classify_price
    from src.pricing.fx import format_inr

    currency = db_conn.execute("SELECT currency FROM products WHERE id = ?", (p_id,)).fetchone()[0]
    inr_price = classify_price(price, currency, None, estimates_enabled=False).amount
    assert inr_price is not None
    faithful_answer_json = json.dumps(
        {
            "summary": "The TrailMaster backpack is an excellent waterproof option.",
            "recommendations": [
                {
                    "product_id": p_id,
                    "external_id": ext_id,
                    "title": title,
                    "why_it_matches": [
                        {
                            "claim": f"Costs {format_inr(inr_price)} and the waterproofing really works in rain.",
                            "evidence_ids": [waterproof_eid],
                        }
                    ],
                    "watch_outs": [],
                }
            ],
            "insufficient_evidence": False,
        }
    )

    llm_service = LLMService(provider=MockLLMProvider([faithful_answer_json]))
    generated_answer = llm_service.generate_answer(
        query="waterproof lightweight backpack",
        products=[product_dict],
        evidence_pack=evidence_pack,
    )

    # 4. Validate through EvidenceValidator
    validator = EvidenceValidator(db_conn, mode="flag")
    validated_answer, report = validator.validate_answer(generated_answer, evidence_pack)

    # 5. Verify outcome
    assert report.total_claims == 1
    assert report.supported_claims == 1
    assert report.unsupported_claims == 0
    assert report.fabricated_citations == 0
    assert report.numeric_mismatches == 0
    assert report.is_valid is True
    assert not validated_answer.recommendations[0].why_it_matches[0].claim.startswith("[UNVERIFIED]")


def test_end_to_end_validation_flag_mode_detects_hallucinations(db_conn: sqlite3.Connection) -> None:
    """Test that flag mode flags fabricated citations and incorrect prices."""
    row = db_conn.execute(
        "SELECT id, external_id, title, description, price FROM products WHERE external_id = 'P001'"
    ).fetchone()
    assert row is not None
    p_id, ext_id, title, desc, real_price = row

    evidence_pack = assemble_evidence_pack(
        conn=db_conn,
        product_ids=[p_id],
        query="waterproof backpack",
        token_budget=1000,
    )
    waterproof_eid = next(
        it.evidence_id for it in evidence_pack.items if "waterproof" in it.text.lower()
    )

    # Hallucinated answer: one supported claim, one fake citation, one wrong price
    hallucinated_json = json.dumps(
        {
            "summary": "Backpack recommendations.",
            "recommendations": [
                {
                    "product_id": p_id,
                    "external_id": ext_id,
                    "title": title,
                    "why_it_matches": [
                        {
                            "claim": "Waterproofing really works in rain.",
                            "evidence_ids": [waterproof_eid],
                        },
                        {
                            "claim": "Features a titanium lock.",
                            "evidence_ids": ["R999_FABRICATED"],
                        },
                        {
                            "claim": "Costs only $9.99 on sale.",  # Real price is 49.99
                            "evidence_ids": [waterproof_eid],
                        },
                    ],
                    "watch_outs": [],
                }
            ],
            "insufficient_evidence": False,
        }
    )

    llm_service = LLMService(provider=MockLLMProvider([hallucinated_json]))
    generated_answer = llm_service.generate_answer(
        query="waterproof backpack",
        products=[{"id": p_id, "external_id": ext_id, "title": title}],
        evidence_pack=evidence_pack,
    )

    validator = EvidenceValidator(db_conn, mode="flag")
    validated_answer, report = validator.validate_answer(generated_answer, evidence_pack)

    assert report.total_claims == 3
    assert report.supported_claims == 1
    assert report.unsupported_claims == 2
    assert report.fabricated_citations == 1
    assert report.numeric_mismatches == 1
    assert report.is_valid is False

    claims = validated_answer.recommendations[0].why_it_matches
    assert claims[0].claim == "Waterproofing really works in rain."
    assert claims[1].claim.startswith("[UNVERIFIED]")
    assert claims[2].claim.startswith("[UNVERIFIED]")


def test_end_to_end_validation_filter_mode_prunes_hallucinations(db_conn: sqlite3.Connection) -> None:
    """Test that filter mode removes unsupported claims and retains supported claims."""
    row = db_conn.execute(
        "SELECT id, external_id, title FROM products WHERE external_id = 'P001'"
    ).fetchone()
    p_id, ext_id, title = row

    evidence_pack = assemble_evidence_pack(
        conn=db_conn,
        product_ids=[p_id],
        query="waterproof backpack",
        token_budget=1000,
    )
    waterproof_eid = next(
        it.evidence_id for it in evidence_pack.items if "waterproof" in it.text.lower()
    )

    # Mixed answer
    answer = StructuredAnswer(
        summary="Mixed claims test.",
        recommendations=[
            ProductRecommendation(
                product_id=p_id,
                external_id=ext_id,
                title=title,
                why_it_matches=[
                    EvidenceClaim(claim="Waterproofing really works in rain.", evidence_ids=[waterproof_eid]),
                    EvidenceClaim(claim="Has built-in microwave oven.", evidence_ids=["R999"]),
                ],
                watch_outs=[
                    EvidenceClaim(claim="Costs only $2.00.", evidence_ids=[waterproof_eid]),  # Numeric mismatch
                ],
            )
        ],
    )

    validator = EvidenceValidator(db_conn, mode="filter")
    filtered_answer, report = validator.validate_answer(answer, evidence_pack)

    assert report.supported_claims == 1
    assert report.unsupported_claims == 2
    assert len(filtered_answer.recommendations) == 1

    rec = filtered_answer.recommendations[0]
    # Only the supported claim remains in why_it_matches
    assert len(rec.why_it_matches) == 1
    assert rec.why_it_matches[0].claim == "Waterproofing really works in rain."
    # Watch outs had only an unsupported claim, so it was filtered out
    assert len(rec.watch_outs) == 0
    assert filtered_answer.insufficient_evidence is False
