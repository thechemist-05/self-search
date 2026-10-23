"""
Report and status data structures for evidence validation in Self Search (Phase 6).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class ValidationStatus(str, Enum):
    """Validation outcome for an individual factual claim."""

    SUPPORTED = "SUPPORTED"
    FABRICATED_CITATION = "FABRICATED_CITATION"
    CROSS_PRODUCT_MISMATCH = "CROSS_PRODUCT_MISMATCH"
    NUMERIC_MISMATCH = "NUMERIC_MISMATCH"
    UNSUPPORTED_LOW_SIMILARITY = "UNSUPPORTED_LOW_SIMILARITY"
    UNVERIFIABLE = "UNVERIFIABLE"


class NumericType(str, Enum):
    """Categorisation of extracted numeric claims."""

    PRICE = "PRICE"
    RATING = "RATING"
    REVIEW_COUNT = "REVIEW_COUNT"
    SPECIFICATION = "SPECIFICATION"


@dataclass
class NumericCheckResult:
    """Detailed audit for a specific numeric claim checked against SQLite ground truth."""

    numeric_type: NumericType
    claimed_value: Any
    database_value: Optional[Any]
    is_match: bool
    status: str  # "VERIFIED_MATCH", "NUMERIC_MISMATCH", "UNVERIFIABLE"
    details: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "numeric_type": self.numeric_type.value,
            "claimed_value": self.claimed_value,
            "database_value": self.database_value,
            "is_match": self.is_match,
            "status": self.status,
            "details": self.details,
        }


@dataclass
class ClaimValidationResult:
    """Validation diagnosis for a single claim in why_it_matches or watch_outs."""

    claim_text: str
    product_id: int
    evidence_ids: list[str]
    status: ValidationStatus
    is_supported: bool
    lexical_score: float = 0.0
    semantic_score: float = 0.0
    numeric_checks: list[NumericCheckResult] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_text": self.claim_text,
            "product_id": self.product_id,
            "evidence_ids": self.evidence_ids,
            "status": self.status.value,
            "is_supported": self.is_supported,
            "lexical_score": round(self.lexical_score, 4),
            "semantic_score": round(self.semantic_score, 4),
            "numeric_checks": [nc.to_dict() for nc in self.numeric_checks],
            "reasons": self.reasons,
        }


@dataclass
class SupportReport:
    """Comprehensive support and audit report generated for a validated answer."""

    query: str
    total_claims: int
    supported_claims: int
    unsupported_claims: int
    fabricated_citations: int
    cross_product_mismatches: int
    numeric_mismatches: int
    unverifiable_claims: int
    support_rate: float
    is_valid: bool
    claim_results: list[ClaimValidationResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "total_claims": self.total_claims,
            "supported_claims": self.supported_claims,
            "unsupported_claims": self.unsupported_claims,
            "fabricated_citations": self.fabricated_citations,
            "cross_product_mismatches": self.cross_product_mismatches,
            "numeric_mismatches": self.numeric_mismatches,
            "unverifiable_claims": self.unverifiable_claims,
            "support_rate": round(self.support_rate, 4),
            "is_valid": self.is_valid,
            "claim_results": [cr.to_dict() for cr in self.claim_results],
        }

    def format_summary(self) -> str:
        """Render a readable text summary of the validation report."""
        lines = [
            "=== Self Search Evidence Support Report ===",
            f"Query: '{self.query}'",
            f"Total Claims Evaluated: {self.total_claims}",
            f"Supported Claims:       {self.supported_claims} ({self.support_rate * 100:.1f}%)",
            f"Unsupported Claims:     {self.unsupported_claims}",
            f"  - Fabricated IDs:     {self.fabricated_citations}",
            f"  - Cross-Product:      {self.cross_product_mismatches}",
            f"  - Numeric Mismatches: {self.numeric_mismatches}",
            f"  - Unverifiable:       {self.unverifiable_claims}",
            f"Validation Status:      {'PASSED' if self.is_valid else 'FLAGGED'}",
        ]
        return "\n".join(lines)
