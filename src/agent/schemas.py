"""
Pydantic schemas for the agent layer (Phase 7).

Defines the structured representation for parsed user queries and agent responses.
These schemas are for *query understanding* only — they do NOT represent verified
product facts. Inferred preferences and exclusions are search signals, not claims.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from src.llm.schemas import GenerationInfo


class UserIntent(str, Enum):
    """Classified intent of a user message."""

    PRODUCT_SEARCH = "PRODUCT_SEARCH"
    REFINE_SEARCH = "REFINE_SEARCH"
    PRODUCT_QUESTION = "PRODUCT_QUESTION"
    COMPARE = "COMPARE"
    CLARIFY = "CLARIFY"
    SMALLTALK = "SMALLTALK"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class ParsedQuery(BaseModel):
    """Structured representation of user intent and constraints.

    WARNING: This is the output of *query understanding*, NOT verified product data.
    ``preferences`` and ``exclusions`` represent what the user asked for, not what
    products actually have. Never treat these as factual product attributes.
    """

    intent: UserIntent = Field(
        ...,
        description="Classified user intent.",
    )
    raw_query: str = Field(
        ...,
        description="Original user message verbatim.",
    )
    search_text: str = Field(
        default="",
        description="Cleaned search phrase suitable for retrieval queries.",
    )
    category: Optional[str] = Field(
        default=None,
        description="Product category extracted from the query (e.g. 'headphones').",
    )
    brand: Optional[str] = Field(
        default=None,
        description="Brand name extracted from the query (e.g. 'Sony').",
    )
    min_price: Optional[float] = Field(
        default=None,
        description="Minimum price constraint.",
    )
    max_price: Optional[float] = Field(
        default=None,
        description="Maximum price constraint.",
    )
    min_rating: Optional[float] = Field(
        default=None,
        description="Minimum star rating constraint (e.g. 4.0).",
    )
    price_currency: Optional[str] = Field(
        default=None,
        description="ISO currency the user stated for min/max price (e.g. 'INR'); None if unstated. "
        "Detected deterministically from the message; never converted.",
    )
    product_types: list[str] = Field(
        default_factory=list,
        description="Product types requested (e.g. ['footwear']); hard constraints checked against "
        "product title/description.",
    )
    colours: list[str] = Field(
        default_factory=list,
        description="Colours the user requires (e.g. ['black'], ['navy']); hard constraints met only by a "
        "colour the listing states. Detected deterministically from the message.",
    )
    materials: list[str] = Field(
        default_factory=list,
        description="Materials the user requires (e.g. ['leather']); hard constraints met only by a "
        "material the listing states. Detected deterministically from the message.",
    )
    occasions: list[str] = Field(
        default_factory=list,
        description="Occasions the user requires (e.g. ['bridal'], ['festive']); hard constraints met only "
        "by an occasion the listing states. Detected deterministically from the message.",
    )
    preferences: list[str] = Field(
        default_factory=list,
        description="User preferences inferred from the query (e.g. ['wireless', 'noise-cancelling']).",
    )
    exclusions: list[str] = Field(
        default_factory=list,
        description="Things the user explicitly wants to avoid (e.g. ['wired', 'Apple']).",
    )
    compare_product_ids: list[int] = Field(
        default_factory=list,
        description="Product IDs for comparison intent.",
    )
    follow_up_context: Optional[str] = Field(
        default=None,
        description="Refinement context from a follow-up message (e.g. 'cheaper', 'only black').",
    )
    clarification_question: Optional[str] = Field(
        default=None,
        description="Focused question the agent should ask the user for clarification.",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Parser confidence score (0.0–1.0). Below 0.5 triggers CLARIFY.",
    )

    def to_metadata_filter_dict(self) -> dict[str, Any]:
        """Extract constraint fields as a dict suitable for MetadataFilter construction.

        Returns only non-None constraint fields.
        """
        result: dict[str, Any] = {}
        if self.category is not None:
            result["category"] = self.category
        if self.brand is not None:
            result["brand"] = self.brand
        if self.min_price is not None:
            result["min_price"] = self.min_price
        if self.max_price is not None:
            result["max_price"] = self.max_price
        if self.min_rating is not None:
            result["min_rating"] = self.min_rating
        if self.colours:
            result["colours"] = tuple(self.colours)
        if self.materials:
            result["materials"] = tuple(self.materials)
        if self.occasions:
            result["occasions"] = tuple(self.occasions)
        return result


class ConstraintItem(BaseModel):
    """One interpreted hard constraint and whether it was enforced."""

    kind: Literal["product_type", "category", "brand", "price", "min_rating", "excluded_brand", "excluded_term",
                  "colour", "material", "occasion"]
    label: str = Field(..., description="Human-readable description, e.g. 'Max price: USD 100.00'.")
    value: Any = None
    status: Literal["applied", "not_applied"] = "applied"
    reason: Optional[str] = Field(default=None, description="Why a constraint was not applied.")
    checked_fields: list[str] = Field(default_factory=list, description="Database fields it is checked against.")


class ConstraintReport(BaseModel):
    """How the request's hard constraints were interpreted and enforced."""

    items: list[ConstraintItem] = Field(default_factory=list)
    matching_products: Optional[int] = Field(
        default=None,
        description="Catalogue products that satisfy every applied constraint (all retrieval modes "
        "return only these).",
    )
    catalog_currencies: list[str] = Field(default_factory=list)
    unknown_attribute_products: Optional[int] = Field(
        default=None,
        description="Products meeting every other constraint but left out because their listing does not "
        "state a required colour or material (never assumed). Null when no colour/material was requested.",
    )


class ConstraintCheck(BaseModel):
    """Evidence from the database that a shown product satisfies one applied constraint."""

    kind: str
    detail: str


class EstimatedPrice(BaseModel):
    """A synthetic INR price estimate (experimental, off by default). Not a retailer price."""

    amount: float
    currency: Literal["INR"] = "INR"
    range_min: float
    range_max: float
    rule: str = Field(..., description="Illustrative category range the estimate came from.")
    config_version: str
    label: str = "Synthetic estimate, not a retailer price"


class PriceConversion(BaseModel):
    """How a source price in another currency was converted to INR (2026-10-23).

    The original amount is kept in the database for traceability and is never sent to
    clients: every price a user sees is in INR.
    """

    from_currency: str = Field(..., description="Currency of the source price, e.g. 'USD'.")
    rate: float = Field(..., description="INR per one unit of from_currency.")
    rate_date: str
    source: str
    source_url: str
    fx_version: str = Field(..., description="Version of config/exchange_rates.toml.")
    label: str = "Converted from the source price at a documented exchange rate"


class ProductCard(BaseModel):
    """Product details for display (Phase 9). Every field is read from the database,
    never from LLM output."""

    id: int
    external_id: Optional[str] = None
    title: str
    brand: Optional[str] = None
    category: Optional[str] = None
    price: Optional[float] = Field(
        default=None,
        description="The source price in INR: an INR source price as supplied, or a source price in "
        "another currency converted with a documented rate (see price_conversion). Never an estimate.",
    )
    currency: Optional[Literal["INR"]] = Field(default=None, description="'INR' whenever price is set.")
    price_status: Optional[Literal["verified", "converted", "estimated", "unknown"]] = Field(
        default=None,
        description="verified: INR source price; converted: source price in another currency, converted "
        "to INR at a documented rate; estimated: no source price, synthetic INR estimate (only when "
        "estimates are enabled); unknown: no usable price ('Price unavailable').",
    )
    price_conversion: Optional[PriceConversion] = Field(
        default=None, description="Present only when price_status is 'converted'.",
    )
    estimated_price: Optional[EstimatedPrice] = Field(
        default=None, description="Present only when price_status is 'estimated'. Never evidence.",
    )
    catalog_rating: Optional[float] = Field(
        default=None, description="avg_rating from the source dataset (not computed from local reviews).",
    )
    catalog_rating_count: Optional[int] = Field(
        default=None, description="rating_count from the source dataset.",
    )
    local_review_count: int = Field(
        default=0, description="Reviews actually stored in the local database for this product.",
    )
    image_url: Optional[str] = None
    image_alternates: list[str] = Field(
        default_factory=list,
        description="Other validated photos of this same product, tried in order if image_url fails.",
    )
    features: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(
        default_factory=dict, description="Other catalogue attributes (from attributes_json).",
    )
    aspects: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Rows from product_aspect_summary. Empty while review intelligence (Phase 2) "
        "is not implemented; never synthesised.",
    )
    recommended: bool = Field(default=False, description="True if the validated answer recommends it.")
    retrieval_rank: Optional[int] = Field(
        default=None, description="1-based position in the products given to the LLM for this answer.",
    )
    constraint_checks: list[ConstraintCheck] = Field(
        default_factory=list,
        description="For search/refine answers: how this product satisfies each applied "
        "constraint, from its database fields. Empty for question/compare turns.",
    )


class EvidenceSnippet(BaseModel):
    """One review snippet from the evidence pack that was validated for this answer."""

    evidence_id: str
    review_id: int
    product_id: int
    title: Optional[str] = None
    text: str
    rating: Optional[float] = None
    verified: bool = False
    helpful_votes: int = 0


class AgentResponse(BaseModel):
    """Response returned from the agent controller to the API layer."""

    session_id: str = Field(
        ...,
        description="Conversation session identifier.",
    )
    intent: UserIntent = Field(
        ...,
        description="Classified intent of the user message.",
    )
    answer: Optional[dict[str, Any]] = Field(
        default=None,
        description="Serialised StructuredAnswer (present for search/question/compare intents).",
    )
    clarification_question: Optional[str] = Field(
        default=None,
        description="Clarification question for the user (present for CLARIFY intent).",
    )
    smalltalk_response: Optional[str] = Field(
        default=None,
        description="Canned response for smalltalk or out-of-scope messages.",
    )
    support_report: Optional[dict[str, Any]] = Field(
        default=None,
        description="Serialised SupportReport from evidence validation.",
    )
    active_filters: dict[str, Any] = Field(
        default_factory=dict,
        description="Current session filter state.",
    )
    shown_product_ids: list[int] = Field(
        default_factory=list,
        description="Product IDs shown in this session so far.",
    )
    parsing_confidence: float = Field(
        default=1.0,
        description="Query parser confidence score.",
    )
    # --- Phase 9 additions (all optional; existing clients can ignore them) ---
    result_product_ids: list[int] = Field(
        default_factory=list,
        description="Products this answer is about, in display order: recommended first (in "
        "recommendation order), then other candidates in retrieval order. Empty for turns "
        "without an answer.",
    )
    products: list[ProductCard] = Field(
        default_factory=list,
        description="Database records for result_product_ids, in the same order.",
    )
    evidence: list[EvidenceSnippet] = Field(
        default_factory=list,
        description="Every item of the evidence pack passed to EvidenceValidator for this answer.",
    )
    generation: Optional[GenerationInfo] = Field(
        default=None,
        description="How the answer was produced ('llm', 'fallback', 'none'). Null without an answer.",
    )
    constraints: Optional[ConstraintReport] = Field(
        default=None,
        description="Interpreted hard constraints for search/refine turns (including a "
        "currency clarification). Null for other turns.",
    )
