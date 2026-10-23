"""
Pydantic schemas for structured LLM output in Self Search (Phase 5).

Strictly matches the output schema defined in docs/master-spec.md Section 10.
"""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator


class GenerationInfo(BaseModel):
    """How an answer was actually produced (Phase 9).

    Not part of the LLM output contract; set by ``LLMService`` from the real outcome.
    """

    mode: Literal["llm", "fallback", "none"] = Field(
        ...,
        description="'llm': validated model output. 'fallback': deterministic template after the "
        "model was unavailable, errored, or kept returning invalid JSON. 'none': the model was "
        "not called (no products retrieved, or a product-first search whose summary was not "
        "requested).",
    )
    reason: Optional[
        Literal[
            "provider_unavailable", "provider_error", "invalid_output_after_retries",
            "unexpected_error", "no_products_retrieved", "summary_not_requested",
        ]
    ] = None
    model: Optional[str] = None
    attempts: Optional[int] = None


class EvidenceClaim(BaseModel):
    """An individual factual claim paired with supporting evidence citation IDs."""

    claim: str = Field(..., description="A factual claim or insight about the product.")
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="List of citation IDs (e.g. ['R1', 'R2']) directly supporting this claim.",
    )


class ProductRecommendation(BaseModel):
    """A recommended candidate product with grounded reasons and watch-outs."""

    product_id: int = Field(..., description="The internal database ID of the product.")
    external_id: Optional[str] = Field(default=None, description="The external ID / ASIN of the product.")
    title: Optional[str] = Field(default=None, description="The product title.")
    why_it_matches: list[EvidenceClaim] = Field(
        default_factory=list,
        description="List of grounded claims explaining why this product satisfies the user need.",
    )
    watch_outs: list[EvidenceClaim] = Field(
        default_factory=list,
        description="Potential caveats, trade-offs, or recurring complaints grounded in reviews.",
    )

    @field_validator("why_it_matches", "watch_outs", mode="before")
    @classmethod
    def _null_means_no_claims(cls, value: object) -> object:
        """Small models often write ``null`` for "none"; that is an empty list, not an error."""
        return [] if value is None else value


class StructuredAnswer(BaseModel):
    """The root structured JSON response contract produced by the LLM layer."""

    summary: str = Field(
        ...,
        description="A concise executive summary addressing the user's inquiry grounded in facts.",
    )
    recommendations: list[ProductRecommendation] = Field(
        default_factory=list,
        description="Top recommended products matching the request.",
    )
    insufficient_evidence: bool = Field(
        default=False,
        description="True if the retrieved evidence is inadequate to make a reliable recommendation.",
    )
    missing_information: Optional[str] = Field(
        default=None,
        description="Specific explanation of what data or evidence is missing when insufficient_evidence is True.",
    )
