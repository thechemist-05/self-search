"""
Database-backed presentation data for the chat UI (Phase 9).

Product cards are read straight from SQLite by product ID. Nothing here uses
LLM output: the answer decides only *which* IDs are recommended, never what
the product's title, price, rating, attributes or image are.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

import structlog

from src.agent.constraints import constraint_checks
from src.agent.schemas import EstimatedPrice, EvidenceSnippet, PriceConversion, ProductCard
from src.data.product_images import (
    alternates_for,
    image_for,
    images_path_for,
    load_image_alternates,
    load_image_map,
)
from src.evidence.evidence_pack import EvidencePack
from src.llm.schemas import StructuredAnswer
from src.pricing.budget import classify_price, load_estimates
from src.retrieval.filters import ESTIMATE_KEY, MetadataFilter

logger = structlog.get_logger("shelfsense.agent.presentation")

MAX_DETAIL_REVIEWS = 20


def order_result_ids(candidate_ids: list[int], answer: Optional[StructuredAnswer]) -> tuple[list[int], set[int]]:
    """Display order: recommended products first (recommendation order), then the rest.

    Recommendations naming products outside ``candidate_ids`` are ignored, so
    the LLM cannot add a product to the result list.

    Returns:
        Tuple of (ordered IDs, set of recommended IDs).
    """
    candidates = list(dict.fromkeys(candidate_ids))
    recommended: list[int] = []
    if answer is not None:
        for rec in answer.recommendations:
            if rec.product_id in candidates and rec.product_id not in recommended:
                recommended.append(rec.product_id)
    rest = [pid for pid in candidates if pid not in recommended]
    return recommended + rest, set(recommended)


def evidence_snippets(pack: Optional[EvidencePack]) -> list[EvidenceSnippet]:
    """Every item of the evidence pack, unchanged, in pack order."""
    if pack is None:
        return []
    return [
        EvidenceSnippet(
            evidence_id=item.evidence_id, review_id=item.review_id, product_id=item.product_id,
            title=item.title or None, text=item.text, rating=item.rating,
            verified=bool(item.verified), helpful_votes=item.helpful_votes or 0,
        )
        for item in pack.items
    ]


def _parse_attributes(raw: Optional[str]) -> tuple[list[str], dict[str, Any]]:
    """Split attributes_json into (features, other attributes). Malformed JSON → empty."""
    if not raw:
        return [], {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return [], {}
    if not isinstance(data, dict):
        return [], {}
    features = data.get("features")
    features = [str(f) for f in features] if isinstance(features, list) else []
    others = {k: v for k, v in data.items() if k != "features"}
    return features, others


class ProductPresenter:
    """Builds ``ProductCard`` objects and product detail views from the database."""

    def __init__(self, conn: sqlite3.Connection, estimates_enabled: Optional[bool] = None) -> None:
        self.conn = conn
        if estimates_enabled is None:
            from src.config.settings import get_settings

            estimates_enabled = get_settings().estimated_prices_enabled
        self.estimates_enabled = bool(estimates_enabled)
        # Image URLs recovered from the source metadata, kept beside the database (see
        # src/data/product_images.py); used only when products.image_url is empty.
        self.images_path = None
        try:
            row = conn.execute("PRAGMA database_list").fetchone()  # (seq, name, file) of "main"
            if row and len(row) > 2 and isinstance(row[2], str) and row[2]:
                self.images_path = images_path_for(row[2])
        except (sqlite3.Error, TypeError):
            pass

    def _image_url(self, product_id: int, external_id: Optional[str], stored: Optional[str]) -> Optional[str]:
        if stored:
            return stored
        if self.images_path is None:
            return None
        return image_for(load_image_map(self.images_path), product_id, external_id)

    def _image_alternates(self, product_id: int, external_id: Optional[str]) -> list[str]:
        if self.images_path is None:
            return []
        return alternates_for(load_image_alternates(self.images_path), product_id, external_id)

    def cards(
        self,
        ordered_ids: list[int],
        recommended_ids: set[int] = frozenset(),  # type: ignore[assignment]
        retrieval_ranks: Optional[dict[int, int]] = None,
        filters: Optional[MetadataFilter] = None,
    ) -> list[ProductCard]:
        """Cards for ``ordered_ids`` in that order. IDs missing from the DB are skipped.

        With ``filters`` (search/refine turns), each card lists how its
        database fields satisfy every applied constraint.
        """
        if not ordered_ids:
            return []
        placeholders = ",".join("?" for _ in ordered_ids)
        rows = self.conn.execute(
            f"""
            SELECT p.id, p.external_id, p.title, b.name, c.name, p.price, p.currency,
                   p.avg_rating, p.rating_count, p.image_url, p.attributes_json,
                   (SELECT COUNT(*) FROM reviews r WHERE r.product_id = p.id),
                   p.description
            FROM products p
            LEFT JOIN brands b ON b.id = p.brand_id
            LEFT JOIN categories c ON c.id = p.category_id
            WHERE p.id IN ({placeholders})
            """,
            list(ordered_ids),
        ).fetchall()
        aspects = self._aspects(list(ordered_ids))
        estimates = load_estimates(self.conn, list(ordered_ids)) if self.estimates_enabled else {}
        by_id = {}
        for r in rows:
            features, attributes = _parse_attributes(r[10])
            view = classify_price(r[5], r[6], estimates.get(r[0]), estimates_enabled=self.estimates_enabled)
            product = {"title": r[2], "description": r[12], "brand_name": r[3], "category_name": r[4],
                       "price": r[5], "currency": r[6], "avg_rating": r[7], "attributes_json": r[10]}
            if view.status == "estimated":
                product[ESTIMATE_KEY] = view.amount
            checks = constraint_checks(product, filters) if filters is not None else []
            est, fx = view.estimate, view.fx
            # Every price on a card is INR: an INR source price, or a documented conversion.
            # The original foreign amount is never put on a card.
            source_inr = view.amount if view.status in ("verified", "converted") else None
            by_id[r[0]] = ProductCard(
                constraint_checks=checks,
                price_status=view.status,
                estimated_price=EstimatedPrice(
                    amount=est.amount, range_min=est.range_min, range_max=est.range_max,
                    rule=est.rule_key, config_version=est.config_version,
                ) if view.status == "estimated" and est is not None else None,
                price_conversion=PriceConversion(
                    from_currency=fx.currency, rate=fx.rate, rate_date=fx.rate_date, source=fx.source,
                    source_url=fx.source_url, fx_version=fx.version,
                ) if view.status == "converted" and fx is not None else None,
                id=r[0], external_id=r[1], title=r[2], brand=r[3], category=r[4], price=source_inr,
                currency="INR" if source_inr is not None else None, catalog_rating=r[7], catalog_rating_count=r[8],
                image_url=self._image_url(r[0], r[1], r[9]), image_alternates=self._image_alternates(r[0], r[1]),
                features=features, attributes=attributes,
                local_review_count=r[11], aspects=aspects.get(r[0], []),
                recommended=r[0] in recommended_ids,
                retrieval_rank=(retrieval_ranks or {}).get(r[0]),
            )
        missing = [pid for pid in ordered_ids if pid not in by_id]
        if missing:
            logger.warning("presentation_products_missing", product_ids=missing)
        return [by_id[pid] for pid in ordered_ids if pid in by_id]

    def _aspects(self, product_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        placeholders = ",".join("?" for _ in product_ids)
        out: dict[int, list[dict[str, Any]]] = {}
        for pid, aspect, pos, neg, neu in self.conn.execute(
            "SELECT product_id, aspect, positive_count, negative_count, neutral_count "
            f"FROM product_aspect_summary WHERE product_id IN ({placeholders}) ORDER BY aspect",
            product_ids,
        ).fetchall():
            out.setdefault(pid, []).append(
                {"aspect": aspect, "positive_count": pos, "negative_count": neg, "neutral_count": neu}
            )
        return out

    def detail(self, product_id: int) -> Optional[dict[str, Any]]:
        """Card plus locally stored reviews (most helpful first), or None if unknown."""
        cards = self.cards([product_id])
        if not cards:
            return None
        reviews = [
            {
                "review_id": r[0], "rating": r[1], "title": r[2], "text": r[3],
                "review_date": r[4], "helpful_votes": r[5] or 0, "verified": bool(r[6]),
            }
            for r in self.conn.execute(
                "SELECT id, rating, title, text, review_date, helpful_votes, verified_flag "
                "FROM reviews WHERE product_id = ? ORDER BY helpful_votes DESC, id LIMIT ?",
                (product_id, MAX_DETAIL_REVIEWS),
            ).fetchall()
        ]
        return {"product": cards[0].model_dump(), "reviews": reviews}
