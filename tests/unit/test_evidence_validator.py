"""Unit tests for Phase 6 Evidence Validation in Self Search."""

from __future__ import annotations

import json
import sqlite3
import pytest

from src.evidence.evidence_pack import EvidenceItem, EvidencePack
from src.evidence.numeric import NumericExtractor, NumericVerifier
from src.evidence.report import (
    ClaimValidationResult,
    NumericCheckResult,
    NumericType,
    SupportReport,
    ValidationStatus,
)
from src.evidence.validator import (
    EvidenceValidator,
    compute_embedding_similarity,
    compute_lexical_overlap,
)
from src.llm.schemas import EvidenceClaim, ProductRecommendation, StructuredAnswer


@pytest.fixture
def test_db() -> sqlite3.Connection:
    """In-memory SQLite database populated with fixture product records."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            external_id TEXT,
            title TEXT,
            description TEXT,
            price REAL,
            currency TEXT,
            avg_rating REAL,
            rating_count INTEGER,
            attributes_json TEXT
        )
        """
    )
    # Product 1: Complete record
    conn.execute(
        """
        INSERT INTO products (id, external_id, title, description, price, currency, avg_rating, rating_count, attributes_json)
        VALUES (
            1,
            'P001',
            'UltraLight Hiking Backpack 40L',
            'Lightweight waterproof backpack for hiking and daily commute.',
            49.99,
            'USD',
            4.3,
            1250,
            ?
        )
        """,
        (json.dumps({"features": ["Waterproof", "40L capacity", "Ripstop nylon"], "weight": "0.8 kg"}),),
    )
    # Product 2: Missing price and rating (NULLs for testing UNVERIFIABLE)
    conn.execute(
        """
        INSERT INTO products (id, external_id, title, description, price, currency, avg_rating, rating_count, attributes_json)
        VALUES (
            2,
            'P002',
            'Prototype Mystery Gadget',
            'Experimental device without pricing.',
            NULL,
            NULL,
            NULL,
            NULL,
            ?
        )
        """,
        (json.dumps({}),),
    )
    # Product 3: USB-C Cable 6ft 100W
    conn.execute(
        """
        INSERT INTO products (id, external_id, title, description, price, currency, avg_rating, rating_count, attributes_json)
        VALUES (
            3,
            'P006',
            'USB-C Fast Charging Cable 6ft',
            'Braided 100W PD charging cable.',
            12.99,
            'USD',
            4.4,
            8900,
            ?
        )
        """,
        (json.dumps({"features": ["100W PD", "6ft length"]}),),
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def sample_evidence_pack() -> EvidencePack:
    """Fixture evidence pack with labelled review items."""
    return EvidencePack(
        query="waterproof backpack",
        items=[
            EvidenceItem(
                evidence_id="R1",
                product_id=1,
                product_external_id="P001",
                review_id=101,
                title="Perfect for hiking",
                text="This backpack is incredibly lightweight yet durable. The waterproofing really works in heavy rain!",
                rating=5.0,
                estimated_tokens=20,
            ),
            EvidenceItem(
                evidence_id="R2",
                product_id=1,
                product_external_id="P001",
                review_id=102,
                title="Straps dig in",
                text="Shoulder straps start to dig in after 4 hours of hiking with full load.",
                rating=3.0,
                estimated_tokens=18,
            ),
            EvidenceItem(
                evidence_id="R3_PROD3",
                product_id=3,
                product_external_id="P006",
                review_id=301,
                title="Super fast",
                text="Charges my laptop at 100W full speed without heating up.",
                rating=5.0,
                estimated_tokens=15,
            ),
        ],
        total_tokens=53,
    )


# ===========================================================================
# 1. Test NumericExtractor
# ===========================================================================
class TestNumericExtractor:
    def test_extract_prices(self) -> None:
        text = "Costs $49.99 normally, discounted to $39, or $1,299.00 for bundle. Also 29.99 dollars."
        prices = NumericExtractor.extract_prices(text)
        assert 49.99 in prices
        assert 39.0 in prices
        assert 1299.0 in prices
        assert 29.99 in prices

    def test_price_extractor_ignores_unrelated_numbers(self) -> None:
        text = "Top 3 products from 2023 version 2 with 5 star features."
        prices = NumericExtractor.extract_prices(text)
        assert len(prices) == 0

    def test_extract_ratings(self) -> None:
        text = "Rated 4.5 stars by users, with 4.8/5 on other sites and 4 out of 5 satisfaction."
        ratings = NumericExtractor.extract_ratings(text)
        assert 4.5 in ratings
        assert 4.8 in ratings
        assert 4.0 in ratings

    def test_extract_counts(self) -> None:
        text = "Backed by 8,900 reviews and over 500 ratings, plus 1200 customer reviews."
        counts = NumericExtractor.extract_counts(text)
        assert 8900 in counts
        assert 500 in counts
        assert 1200 in counts

    def test_extract_specs(self) -> None:
        text = "Delivers 100W with 20000mAh capacity in a 40L backpack with 6ft cable and 5 lbs weight."
        specs = NumericExtractor.extract_specs(text)
        spec_dict = {unit.lower(): val for val, unit in specs}
        assert spec_dict.get("w") == "100"
        assert spec_dict.get("mah") == "20000"
        assert spec_dict.get("l") == "40"
        assert spec_dict.get("ft") == "6"
        assert spec_dict.get("lbs") == "5"


# ===========================================================================
# 2. Test NumericVerifier and Tolerances
# ===========================================================================
class TestNumericVerifier:
    """Prices are checked in INR (2026-10-23): product 1's source price is USD 49.99, which is
    ₹4,153 at the documented rate (49.99 × 83.0815, whole rupees)."""

    def test_exact_price_match(self, test_db: sqlite3.Connection) -> None:
        verifier = NumericVerifier()
        res = verifier.verify_claims(test_db, product_id=1, claim_text="Priced at ₹4,153.")
        assert len(res) == 1
        assert res[0].is_match is True
        assert res[0].status == "VERIFIED_MATCH"

    def test_price_within_tolerance(self, test_db: sqlite3.Connection) -> None:
        # ₹4,154 vs ₹4,153: within the one-rupee tolerance; "Rs." and "rupees" are INR too
        verifier = NumericVerifier()
        for text in ("Costs around ₹4,154.", "Costs Rs. 4153.", "About 4,152 rupees."):
            res = verifier.verify_claims(test_db, product_id=1, claim_text=text)
            assert len(res) == 1 and res[0].status == "VERIFIED_MATCH", text

    def test_price_outside_tolerance_is_mismatch(self, test_db: sqlite3.Connection) -> None:
        verifier = NumericVerifier()
        res = verifier.verify_claims(test_db, product_id=1, claim_text="Costs only ₹1,999.")
        assert len(res) == 1
        assert res[0].is_match is False
        assert res[0].status == "NUMERIC_MISMATCH"

    def test_source_currency_amount_is_always_a_mismatch(self, test_db: sqlite3.Connection) -> None:
        # Even the true source amount: prices are shown only in INR, never in dollars.
        verifier = NumericVerifier()
        res = verifier.verify_claims(test_db, product_id=1, claim_text="Priced at $49.99.")
        assert len(res) == 1
        assert res[0].is_match is False and res[0].status == "NUMERIC_MISMATCH"
        assert "only in INR" in res[0].details

    def test_missing_db_price_produces_unverifiable(self, test_db: sqlite3.Connection) -> None:
        # Product 2 has NULL price
        verifier = NumericVerifier()
        res = verifier.verify_claims(test_db, product_id=2, claim_text="Priced at ₹6,999.")
        assert len(res) == 1
        assert res[0].is_match is True
        assert res[0].status == "UNVERIFIABLE"
        assert "price unavailable" in res[0].details

    def test_rating_tolerance_and_mismatch(self, test_db: sqlite3.Connection) -> None:
        # DB has 4.3 rating
        verifier = NumericVerifier(rating_tolerance=0.15)
        # 4.4 is within 0.15 tolerance
        res_pass = verifier.verify_claims(test_db, product_id=1, claim_text="Holds a 4.4 star rating.")
        assert res_pass[0].is_match is True
        assert res_pass[0].status == "VERIFIED_MATCH"

        # 2.5 is far outside tolerance
        res_fail = verifier.verify_claims(test_db, product_id=1, claim_text="Holds a 2.5 star rating.")
        assert res_fail[0].is_match is False
        assert res_fail[0].status == "NUMERIC_MISMATCH"

    def test_review_count_relative_tolerance(self, test_db: sqlite3.Connection) -> None:
        # DB has 1250 reviews. 20% tolerance allows 1000 to 1500.
        verifier = NumericVerifier(count_relative_tolerance=0.20)
        res_pass = verifier.verify_claims(test_db, product_id=1, claim_text="Over 1300 reviews.")
        assert res_pass[0].is_match is True
        assert res_pass[0].status == "VERIFIED_MATCH"

        res_fail = verifier.verify_claims(test_db, product_id=1, claim_text="Over 50,000 reviews.")
        assert res_fail[0].is_match is False
        assert res_fail[0].status == "NUMERIC_MISMATCH"

    def test_specification_verification(self, test_db: sqlite3.Connection) -> None:
        verifier = NumericVerifier()
        # Product 1 has '40L capacity' in attributes
        res_match = verifier.verify_claims(test_db, product_id=1, claim_text="Offers 40L capacity.")
        assert res_match[0].is_match is True
        assert res_match[0].status == "VERIFIED_MATCH"

        # Product 1 does NOT have 100W
        res_unverifiable = verifier.verify_claims(test_db, product_id=1, claim_text="Supports 100W charging.")
        assert res_unverifiable[0].is_match is True
        assert res_unverifiable[0].status == "UNVERIFIABLE"

    def test_missing_product_produces_unverifiable(self, test_db: sqlite3.Connection) -> None:
        verifier = NumericVerifier()
        # Product 999 does not exist in DB
        res = verifier.verify_claims(test_db, product_id=999, claim_text="Costs ₹999 with 4.5 stars.")
        assert len(res) == 2
        assert all(r.status == "UNVERIFIABLE" for r in res)
        assert all(r.is_match is True for r in res)


# ===========================================================================
# 3. Test EvidenceValidator - Citations and Similarity
# ===========================================================================
class TestEvidenceValidatorCitations:
    def test_valid_supported_claim(
        self, test_db: sqlite3.Connection, sample_evidence_pack: EvidencePack
    ) -> None:
        validator = EvidenceValidator(test_db, mode="flag")
        answer = StructuredAnswer(
            summary="A great waterproof backpack.",
            recommendations=[
                ProductRecommendation(
                    product_id=1,
                    external_id="P001",
                    title="UltraLight Hiking Backpack 40L",
                    why_it_matches=[
                        EvidenceClaim(
                            claim="The waterproofing holds up great in rain.",
                            evidence_ids=["R1"],
                        )
                    ],
                    watch_outs=[
                        EvidenceClaim(
                            claim="Shoulder straps start to dig in on long hikes.",
                            evidence_ids=["R2"],
                        )
                    ],
                )
            ],
        )

        sanitized, report = validator.validate_answer(answer, sample_evidence_pack)
        assert report.total_claims == 2
        assert report.supported_claims == 2
        assert report.unsupported_claims == 0
        assert report.fabricated_citations == 0
        assert report.is_valid is True
        assert not sanitized.recommendations[0].why_it_matches[0].claim.startswith("[UNVERIFIED]")

    def test_fabricated_citation_id_flagged(
        self, test_db: sqlite3.Connection, sample_evidence_pack: EvidencePack
    ) -> None:
        validator = EvidenceValidator(test_db, mode="flag")
        answer = StructuredAnswer(
            summary="Recommendation with fake citation.",
            recommendations=[
                ProductRecommendation(
                    product_id=1,
                    external_id="P001",
                    title="Backpack",
                    why_it_matches=[
                        EvidenceClaim(
                            claim="Super durable canvas material.",
                            evidence_ids=["R999_DOES_NOT_EXIST"],
                        )
                    ],
                )
            ],
        )

        sanitized, report = validator.validate_answer(answer, sample_evidence_pack)
        assert report.total_claims == 1
        assert report.supported_claims == 0
        assert report.unsupported_claims == 1
        assert report.fabricated_citations == 1
        assert report.is_valid is False
        assert sanitized.recommendations[0].why_it_matches[0].claim.startswith("[UNVERIFIED]")

    def test_cross_product_citation_flagged(
        self, test_db: sqlite3.Connection, sample_evidence_pack: EvidencePack
    ) -> None:
        # Citing R3_PROD3 (which belongs to Product 3) for Product 1
        validator = EvidenceValidator(test_db, mode="flag")
        answer = StructuredAnswer(
            summary="Cross product mismatch.",
            recommendations=[
                ProductRecommendation(
                    product_id=1,
                    external_id="P001",
                    title="Backpack",
                    why_it_matches=[
                        EvidenceClaim(
                            claim="Charges fast at 100W.",
                            evidence_ids=["R3_PROD3"],
                        )
                    ],
                )
            ],
        )

        sanitized, report = validator.validate_answer(answer, sample_evidence_pack)
        assert report.cross_product_mismatches == 1
        assert report.is_valid is False
        assert report.claim_results[0].status == ValidationStatus.CROSS_PRODUCT_MISMATCH

    def test_uncited_claim_flagged(
        self, test_db: sqlite3.Connection, sample_evidence_pack: EvidencePack
    ) -> None:
        validator = EvidenceValidator(test_db, mode="flag")
        answer = StructuredAnswer(
            summary="Uncited claim.",
            recommendations=[
                ProductRecommendation(
                    product_id=1,
                    external_id="P001",
                    title="Backpack",
                    why_it_matches=[
                        EvidenceClaim(
                            claim="Has internal solar heating panels.",
                            evidence_ids=[],
                        )
                    ],
                )
            ],
        )

        sanitized, report = validator.validate_answer(answer, sample_evidence_pack)
        assert report.unsupported_claims == 1
        assert report.claim_results[0].status == ValidationStatus.UNSUPPORTED_LOW_SIMILARITY


# ===========================================================================
# 4. Test Flag Mode vs Filter Mode
# ===========================================================================
class TestFlagVsFilterMode:
    def test_flag_mode_annotates_unsupported_without_modifying_supported(
        self, test_db: sqlite3.Connection, sample_evidence_pack: EvidencePack
    ) -> None:
        validator = EvidenceValidator(test_db, mode="flag")
        answer = StructuredAnswer(
            summary="Comparison.",
            recommendations=[
                ProductRecommendation(
                    product_id=1,
                    external_id="P001",
                    title="Backpack",
                    why_it_matches=[
                        EvidenceClaim(claim="The waterproofing really works.", evidence_ids=["R1"]),
                        EvidenceClaim(claim="Costs only $5.00.", evidence_ids=["R1"]),  # Price mismatch
                    ],
                )
            ],
        )

        sanitized, report = validator.validate_answer(answer, sample_evidence_pack)
        rec = sanitized.recommendations[0]
        # First claim is supported, completely unchanged
        assert rec.why_it_matches[0].claim == "The waterproofing really works."
        # Second claim is flagged, and its dollar amount is never shown
        assert rec.why_it_matches[1].claim.startswith("[UNVERIFIED]")
        assert "$" not in rec.why_it_matches[1].claim and "price in ₹ shown on the card" in rec.why_it_matches[1].claim
        assert all("$" not in r.claim_text for r in report.claim_results)

    def test_filter_mode_prunes_unsupported_claims(
        self, test_db: sqlite3.Connection, sample_evidence_pack: EvidencePack
    ) -> None:
        validator = EvidenceValidator(test_db, mode="filter")
        answer = StructuredAnswer(
            summary="Comparison.",
            recommendations=[
                ProductRecommendation(
                    product_id=1,
                    external_id="P001",
                    title="Backpack",
                    why_it_matches=[
                        EvidenceClaim(claim="The waterproofing really works.", evidence_ids=["R1"]),
                        EvidenceClaim(claim="Costs only $5.00.", evidence_ids=["R1"]),  # Price mismatch
                    ],
                )
            ],
        )

        sanitized, report = validator.validate_answer(answer, sample_evidence_pack)
        rec = sanitized.recommendations[0]
        # Unsupported claim was filtered out
        assert len(rec.why_it_matches) == 1
        # Supported claim is preserved exactly
        assert rec.why_it_matches[0].claim == "The waterproofing really works."

    def test_filter_mode_all_unsupported_returns_insufficient_evidence(
        self, test_db: sqlite3.Connection, sample_evidence_pack: EvidencePack
    ) -> None:
        validator = EvidenceValidator(test_db, mode="filter")
        answer = StructuredAnswer(
            summary="All fake.",
            recommendations=[
                ProductRecommendation(
                    product_id=1,
                    external_id="P001",
                    title="Backpack",
                    why_it_matches=[
                        EvidenceClaim(claim="Costs only $5.00.", evidence_ids=["FAKE_ID"]),
                    ],
                )
            ],
        )

        sanitized, report = validator.validate_answer(answer, sample_evidence_pack)
        assert sanitized.insufficient_evidence is True
        assert len(sanitized.recommendations) == 0
        assert "insufficient evidence" in sanitized.summary.lower() or "verifiable" in sanitized.summary.lower()


# ===========================================================================
# 5. Test SupportReport Formatting and Metrics
# ===========================================================================
class TestSupportReport:
    def test_support_report_metrics(self) -> None:
        report = SupportReport(
            query="test query",
            total_claims=4,
            supported_claims=3,
            unsupported_claims=1,
            fabricated_citations=0,
            cross_product_mismatches=0,
            numeric_mismatches=1,
            unverifiable_claims=1,
            support_rate=0.75,
            is_valid=False,
        )
        assert report.support_rate == 0.75
        assert report.is_valid is False
        summary = report.format_summary()
        assert "75.0%" in summary
        assert "Numeric Mismatches: 1" in summary
        assert "FLAGGED" in summary

    def test_lexical_and_semantic_overlap_helpers(self) -> None:
        t1 = "waterproof hiking backpack"
        t2 = "The waterproofing works great on hiking backpacks."
        overlap = compute_lexical_overlap(t1, t2)
        assert overlap > 0.0

        sem_sim = compute_embedding_similarity(t1, t2)
        assert -1.0 <= sem_sim <= 1.0
