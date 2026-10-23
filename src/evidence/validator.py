"""
Evidence Validator for Self Search (Phase 6).

Scrutinises structured LLM answers against supplied evidence packs and the SQLite catalog:
1. Validates that citation IDs exist and belong to the correct candidate product.
2. Evaluates lexical and semantic relevance as supporting signals (with documented heuristic limits).
3. Recomputes numeric claims (prices, ratings, counts, specs) against database ground truth.
4. Generates an auditable SupportReport and operates in 'flag' (default) or 'filter' mode.
"""

from __future__ import annotations

import math
import re
import sqlite3
from typing import Any, Literal, Optional

import structlog

from src.embeddings.embeddings import get_local_or_remote_embedding
from src.evidence.evidence_pack import EvidenceItem, EvidencePack
from src.evidence.numeric import NumericVerifier
from src.pricing.redact import redact_foreign_prices
from src.evidence.report import (
    ClaimValidationResult,
    NumericCheckResult,
    SupportReport,
    ValidationStatus,
)
from src.llm.schemas import EvidenceClaim, ProductRecommendation, StructuredAnswer

logger = structlog.get_logger("shelfsense.evidence.validator")

STOPWORDS = {
    "a", "an", "the", "and", "or", "in", "on", "at", "to", "for", "with", "is",
    "are", "was", "were", "it", "this", "that", "of", "by", "as", "from", "be",
    "has", "have", "had", "my", "your", "its", "so", "very", "too", "also",
}


def compute_lexical_overlap(text1: str, text2: str) -> float:
    """Compute Jaccard token overlap over content words (heuristic signal)."""
    tokens1 = set(re.findall(r"\b[A-Za-z0-9_\-]+\b", text1.lower())) - STOPWORDS
    tokens2 = set(re.findall(r"\b[A-Za-z0-9_\-]+\b", text2.lower())) - STOPWORDS
    if not tokens1 or not tokens2:
        return 0.0
    intersection = tokens1 & tokens2
    union = tokens1 | tokens2
    return len(intersection) / len(union)


def compute_embedding_similarity(text1: str, text2: str) -> float:
    """Compute cosine similarity using normalized embedding vectors (heuristic signal)."""
    vec1 = get_local_or_remote_embedding(text1)
    vec2 = get_local_or_remote_embedding(text2)
    # Both vectors are unit L2 normalized, so dot product is cosine similarity
    dot = sum(a * b for a, b in zip(vec1, vec2))
    return max(-1.0, min(1.0, float(dot)))


class EvidenceValidator:
    """Verifies evidence citations, factual claims, and numeric assertions."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        mode: Literal["flag", "filter"] = "flag",
        min_semantic_similarity: float = 0.35,
        numeric_verifier: Optional[NumericVerifier] = None,
    ) -> None:
        self.conn = conn
        self.mode = mode
        self.min_semantic_similarity = min_semantic_similarity
        self.numeric_verifier = numeric_verifier or NumericVerifier()

    def validate_answer(
        self,
        answer: StructuredAnswer,
        evidence_pack: Optional[EvidencePack] = None,
    ) -> tuple[StructuredAnswer, SupportReport]:
        """Validate all recommendations in a StructuredAnswer.

        Args:
            answer: The generated StructuredAnswer.
            evidence_pack: The supplied review evidence pack (contains valid citation IDs).

        Returns:
            Tuple of (sanitized_or_flagged_answer, support_report).
        """
        evidence_map: dict[str, EvidenceItem] = {}
        if evidence_pack:
            for item in evidence_pack.items:
                evidence_map[item.evidence_id] = item

        claim_results: list[ClaimValidationResult] = []
        total_claims = 0
        supported_claims = 0
        unsupported_claims = 0
        fabricated_citations = 0
        cross_product_mismatches = 0
        numeric_mismatches = 0
        unverifiable_claims = 0

        validated_recommendations: list[ProductRecommendation] = []

        for rec in answer.recommendations:
            valid_why_matches: list[EvidenceClaim] = []
            valid_watchouts: list[EvidenceClaim] = []

            def _process_claim(claim: EvidenceClaim, target_list: list[EvidenceClaim]) -> None:
                nonlocal total_claims, supported_claims, unsupported_claims
                nonlocal fabricated_citations, cross_product_mismatches, numeric_mismatches, unverifiable_claims

                total_claims += 1
                res = self._validate_single_claim(rec.product_id, claim, evidence_map)
                # A foreign-currency amount was judged above (a numeric mismatch); it is never shown.
                safe_text = redact_foreign_prices(claim.claim)
                if safe_text != claim.claim:
                    res.claim_text = safe_text
                    claim = EvidenceClaim(claim=safe_text, evidence_ids=claim.evidence_ids)
                claim_results.append(res)

                if res.status == ValidationStatus.UNVERIFIABLE or any(
                    nc.status == "UNVERIFIABLE" for nc in res.numeric_checks
                ):
                    unverifiable_claims += 1

                if res.is_supported:
                    supported_claims += 1
                    target_list.append(claim)
                else:
                    unsupported_claims += 1
                    if res.status == ValidationStatus.FABRICATED_CITATION:
                        fabricated_citations += 1
                    elif res.status == ValidationStatus.CROSS_PRODUCT_MISMATCH:
                        cross_product_mismatches += 1
                    elif res.status == ValidationStatus.NUMERIC_MISMATCH:
                        numeric_mismatches += 1

                    if self.mode == "flag":
                        # In flag mode, annotate unsupported claim with [UNVERIFIED]
                        annotated = EvidenceClaim(
                            claim=f"[UNVERIFIED] {claim.claim}",
                            evidence_ids=claim.evidence_ids,
                        )
                        target_list.append(annotated)

            for claim in rec.why_it_matches:
                _process_claim(claim, valid_why_matches)

            for claim in rec.watch_outs:
                _process_claim(claim, valid_watchouts)

            # In filter mode, keep recommendation only if it has supported claims
            if self.mode == "filter":
                if valid_why_matches or valid_watchouts:
                    rec_copy = ProductRecommendation(
                        product_id=rec.product_id,
                        external_id=rec.external_id,
                        title=redact_foreign_prices(rec.title) if rec.title else rec.title,
                        why_it_matches=valid_why_matches,
                        watch_outs=valid_watchouts,
                    )
                    validated_recommendations.append(rec_copy)
            else:
                rec_copy = ProductRecommendation(
                    product_id=rec.product_id,
                    external_id=rec.external_id,
                    title=redact_foreign_prices(rec.title) if rec.title else rec.title,
                    why_it_matches=valid_why_matches,
                    watch_outs=valid_watchouts,
                )
                validated_recommendations.append(rec_copy)

        support_rate = (supported_claims / total_claims) if total_claims > 0 else 1.0
        # Valid if support rate >= 70% and zero fabricated IDs, cross-product, or numeric mismatches
        is_valid = (
            (support_rate >= 0.70)
            and (fabricated_citations == 0)
            and (cross_product_mismatches == 0)
            and (numeric_mismatches == 0)
        )

        report = SupportReport(
            query=evidence_pack.query if evidence_pack else "",
            total_claims=total_claims,
            supported_claims=supported_claims,
            unsupported_claims=unsupported_claims,
            fabricated_citations=fabricated_citations,
            cross_product_mismatches=cross_product_mismatches,
            numeric_mismatches=numeric_mismatches,
            unverifiable_claims=unverifiable_claims,
            support_rate=support_rate,
            is_valid=is_valid,
            claim_results=claim_results,
        )

        # Output answer synthesis
        if self.mode == "filter" and not validated_recommendations and total_claims > 0:
            final_answer = StructuredAnswer(
                summary="No recommendations with verifiable supporting evidence remain.",
                recommendations=[],
                insufficient_evidence=True,
                missing_information="All generated claims failed evidence verification.",
            )
        else:
            # The summary is not claim-checked, but it is never allowed to show a foreign price.
            final_answer = StructuredAnswer(
                summary=redact_foreign_prices(answer.summary),
                recommendations=validated_recommendations,
                insufficient_evidence=answer.insufficient_evidence,
                missing_information=redact_foreign_prices(answer.missing_information or "") or answer.missing_information,
            )

        logger.info(
            "evidence_validation_complete",
            total_claims=total_claims,
            supported=supported_claims,
            support_rate=round(support_rate, 4),
            is_valid=is_valid,
        )

        return final_answer, report

    def _validate_single_claim(
        self,
        product_id: int,
        claim: EvidenceClaim,
        evidence_map: dict[str, EvidenceItem],
    ) -> ClaimValidationResult:
        """Validate one claim against citations, similarity signals, and numeric recomputations."""
        reasons: list[str] = []
        status = ValidationStatus.SUPPORTED

        # 1. Numeric Recomputation from SQLite ground truth
        numeric_checks = self.numeric_verifier.verify_claims(self.conn, product_id, claim.claim)
        has_numeric_mismatch = False
        has_unverifiable_numeric = False
        has_verified_numeric = False

        for nc in numeric_checks:
            if nc.status == "NUMERIC_MISMATCH":
                has_numeric_mismatch = True
                reasons.append(nc.details)
            elif nc.status == "UNVERIFIABLE":
                has_unverifiable_numeric = True
                reasons.append(nc.details)
            elif nc.status == "VERIFIED_MATCH":
                has_verified_numeric = True

        if has_numeric_mismatch:
            status = ValidationStatus.NUMERIC_MISMATCH

        # 2. Citation verification
        max_lex = 0.0
        max_sem = 0.0

        if claim.evidence_ids:
            for eid in claim.evidence_ids:
                if eid not in evidence_map:
                    status = ValidationStatus.FABRICATED_CITATION
                    reasons.append(f"Citation ID '{eid}' does not exist in the evidence pack.")
                    continue

                item = evidence_map[eid]
                if item.product_id != product_id:
                    status = ValidationStatus.CROSS_PRODUCT_MISMATCH
                    reasons.append(
                        f"Citation '{eid}' belongs to Product {item.product_id}, but was cited for Product {product_id}."
                    )
                    continue

                # Grounding checks (heuristic supporting signals)
                lex = compute_lexical_overlap(claim.claim, item.text)
                sem = compute_embedding_similarity(claim.claim, item.text)
                max_lex = max(max_lex, lex)
                max_sem = max(max_sem, sem)

            # If citations were present and none of them showed any semantic or lexical connection
            if (
                status in (ValidationStatus.SUPPORTED, ValidationStatus.UNVERIFIABLE)
                and max_sem < self.min_semantic_similarity
                and max_lex < 0.05
            ):
                status = ValidationStatus.UNSUPPORTED_LOW_SIMILARITY
                reasons.append(
                    f"Cited snippet has weak relevance to the claim (semantic similarity: {max_sem:.2f}, lexical: {max_lex:.2f})."
                )
        else:
            # No citations provided
            if has_verified_numeric and status != ValidationStatus.NUMERIC_MISMATCH:
                status = ValidationStatus.SUPPORTED
            elif has_unverifiable_numeric and status != ValidationStatus.NUMERIC_MISMATCH:
                status = ValidationStatus.UNVERIFIABLE
            else:
                status = ValidationStatus.UNSUPPORTED_LOW_SIMILARITY
                reasons.append("No evidence citations provided to support this claim.")

        # If no severe errors occurred, but numeric values were unverifiable
        if status == ValidationStatus.SUPPORTED and has_unverifiable_numeric:
            status = ValidationStatus.UNVERIFIABLE

        is_supported = status in (ValidationStatus.SUPPORTED, ValidationStatus.UNVERIFIABLE)

        return ClaimValidationResult(
            claim_text=claim.claim,
            product_id=product_id,
            evidence_ids=claim.evidence_ids,
            status=status,
            is_supported=is_supported,
            lexical_score=max_lex,
            semantic_score=max_sem,
            numeric_checks=numeric_checks,
            reasons=reasons,
        )
