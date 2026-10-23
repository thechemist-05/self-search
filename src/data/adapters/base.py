"""Shared adapter interface: source records in, normalised records or explicit rejections out.

Adapters only transform what a source actually contains. They never fill a missing
value: an absent price, currency, brand, rating or description stays ``None``.
Reviewer identifiers and other personal data are dropped inside the adapter, so
they never reach the pipeline or the database.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Optional, Union


@dataclass(frozen=True)
class SourceInfo:
    """Provenance recorded in ``data_sources``."""

    name: str
    url: str
    license: str
    version: str
    retrieved_at: Optional[str] = None
    files: tuple[tuple[str, str], ...] = ()  # (file name, sha256)
    notes: str = ""


@dataclass(frozen=True)
class ProductRecord:
    """A product exactly as the source describes it (missing values stay None)."""

    external_id: str
    title: str
    description: Optional[str] = None
    category_path: tuple[str, ...] = ()
    brand: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = None
    avg_rating: Optional[float] = None
    rating_count: Optional[int] = None
    attributes: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewRecord:
    """One individual review, linked to its product by the source's own identifier."""

    external_id: str
    product_external_id: str
    text: str
    title: Optional[str] = None
    rating: Optional[float] = None
    review_date: Optional[str] = None
    helpful_votes: int = 0
    verified: bool = False
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Rejection:
    """A source record that was not ingested, with an explicit reason."""

    kind: str  # "product" | "review"
    reason: str
    source_ref: str  # line number or identifier, never personal data
    detail: str = ""


ProductResult = Union[ProductRecord, Rejection]
ReviewResult = Union[ReviewRecord, Rejection]


class SourceAdapter(ABC):
    """One source's transformation rules."""

    @abstractmethod
    def source_info(self) -> SourceInfo:
        """Provenance for this source."""

    @abstractmethod
    def parse_products(self, records: Iterable[dict[str, Any]]) -> Iterator[ProductResult]:
        """Turn raw product records into ``ProductRecord`` or ``Rejection``."""

    def parse_reviews(self, records: Iterable[dict[str, Any]]) -> Iterator[ReviewResult]:
        """Turn raw review records into ``ReviewRecord`` or ``Rejection`` (none by default)."""
        return iter(())
