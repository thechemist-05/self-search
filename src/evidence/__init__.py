"""Evidence assembly and validation module."""

from src.evidence.evidence_pack import (
    EvidenceItem,
    EvidencePack,
    assemble_evidence_pack,
    estimate_tokens,
)
from src.evidence.numeric import (
    NumericExtractor,
    NumericVerifier,
)
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

__all__ = [
    "ClaimValidationResult",
    "EvidenceItem",
    "EvidencePack",
    "EvidenceValidator",
    "NumericCheckResult",
    "NumericExtractor",
    "NumericType",
    "NumericVerifier",
    "SupportReport",
    "ValidationStatus",
    "assemble_evidence_pack",
    "compute_embedding_similarity",
    "compute_lexical_overlap",
    "estimate_tokens",
]
