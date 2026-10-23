"""Adapters + pipeline on hand-written fixtures: validation, dedup, provenance, exact review matching."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.data.adapters.abo import AboAdapter
from src.data.adapters.amazon_reviews_2023 import AmazonReviews2023Adapter
from src.data.adapters.base import ProductRecord, Rejection, ReviewRecord
from src.data.adapters.pipeline import IngestReport, ingest, read_jsonl

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "adapters"
META, REVIEWS, ABO = FIX / "amazon2023_meta.jsonl", FIX / "amazon2023_reviews.jsonl", FIX / "abo_listings.jsonl"


def amazon() -> AmazonReviews2023Adapter:
    return AmazonReviews2023Adapter(revision="fixture-rev", files=(("meta.jsonl", "aa" * 32),),
                                    retrieved_at="2026-10-21T00:00:00Z")


def run_amazon(conn: sqlite3.Connection, **kw) -> IngestReport:
    return ingest(conn, amazon(), read_jsonl(META), read_jsonl(REVIEWS), **kw)


def product(conn, ext: str) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT p.*, b.name AS brand, c.name AS category FROM products p "
                       "LEFT JOIN brands b ON b.id = p.brand_id LEFT JOIN categories c ON c.id = p.category_id "
                       "WHERE external_id = ?", (ext,)).fetchone()
    conn.row_factory = None
    return row


class TestAmazonAdapter:
    def test_products(self) -> None:
        out = list(amazon().parse_products(read_jsonl(META)))
        ok = {r.external_id: r for r in out if isinstance(r, ProductRecord)}
        rejected = sorted(r.reason for r in out if isinstance(r, Rejection))
        assert rejected == ["invalid_average_rating", "malformed_record", "missing_parent_asin", "missing_title"]
        snk = ok["TST-SNK-001"]
        assert (snk.price, snk.currency, snk.brand) == (49.99, "USD", "Fixturo")
        assert snk.attributes["store"] == "Fixturo Store" and snk.attributes["brand_source"] == "details.Brand"
        assert snk.category_path[-1] == "Fashion Sneakers" and snk.description == "A test shoe."
        assert not {"images", "videos", "bought_together"} & set(snk.attributes)

    def test_missing_values_stay_missing(self) -> None:
        ok = {r.external_id: r for r in amazon().parse_products(read_jsonl(META)) if isinstance(r, ProductRecord)}
        ear = ok["TST-EAR-001"]
        assert ear.brand is None and ear.price is None and ear.currency is None  # store is not a brand
        vase = ok["TST-VAS-001"]
        assert (vase.avg_rating, vase.rating_count, vase.description) == (None, None, None)
        bag = ok["TST-BAG-001"]
        assert bag.price is None and bag.attributes["price_raw"] == "from 19.99"  # never parsed as a price
        zero = ok["TST-ZER-001"]
        assert zero.price is None and zero.attributes["price_raw"] == "0"

    def test_reviews_drop_personal_data(self) -> None:
        out = list(amazon().parse_reviews(read_jsonl(REVIEWS)))
        reviews = [r for r in out if isinstance(r, ReviewRecord)]
        dumped = json.dumps([r.__dict__ for r in reviews])
        assert "AFAKEUSERID" not in dumped and "user_id" not in dumped and "images" not in dumped
        first = reviews[0]
        assert first.text == "Very comfortable for daily walks. Good grip." and first.verified is True
        assert first.review_date == "2023-03-28" and first.rating == 5.0
        assert sorted(r.reason for r in out if isinstance(r, Rejection)) == \
            ["invalid_rating", "malformed_record", "missing_parent_asin", "missing_text"]

    def test_review_ids_are_stable(self) -> None:
        a = [r.external_id for r in amazon().parse_reviews(read_jsonl(REVIEWS)) if isinstance(r, ReviewRecord)]
        b = [r.external_id for r in amazon().parse_reviews(read_jsonl(REVIEWS)) if isinstance(r, ReviewRecord)]
        assert a == b and all(x.startswith("ar23-") for x in a)


class TestAboAdapter:
    def test_amazon_in_only_and_english_titles(self) -> None:
        out = list(AboAdapter(version="fixture").parse_products(read_jsonl(ABO)))
        ok = {r.external_id: r for r in out if isinstance(r, ProductRecord)}
        assert sorted(r.reason for r in out if isinstance(r, Rejection)) == \
            ["missing_english_title", "missing_item_id", "out_of_scope_marketplace"]
        shoe = ok["B0CROSS0001"]
        assert shoe.title == "Testline Men's Running Shoes" and shoe.brand == "Testline"
        assert shoe.category_path == ("Categories", "Shoes & Handbags", "Men's Shoes", "Sports Shoes")
        assert shoe.attributes["title_hi"].startswith("टेस्टलाइन") and shoe.attributes["color"] == "Black"
        assert (shoe.price, shoe.currency, shoe.avg_rating, shoe.rating_count) == (None, None, None, None)
        assert ok["B0USTITLE01"].attributes["title_language"] == "en_US"
        assert ok["B0USTITLE01"].category_path == ("BACKPACK",)

    def test_no_reviews(self) -> None:
        assert list(AboAdapter(version="x").parse_reviews([{"anything": 1}])) == []


class TestPipeline:
    def test_counts_and_rejection_reasons(self, catalogue_db: Path, tmp_path: Path) -> None:
        conn = sqlite3.connect(catalogue_db)
        report = run_amazon(conn)
        assert (report.products_read, report.products_inserted) == (10, 6)
        assert report.reviews_inserted == 4
        assert report.rejections == {
            "product:duplicate_in_source": 1, "product:invalid_average_rating": 1, "product:malformed_record": 1,
            "product:missing_parent_asin": 1, "product:missing_title": 1,
            "review:duplicate_in_source": 1, "review:invalid_rating": 1, "review:malformed_record": 1,
            "review:missing_parent_asin": 1, "review:missing_text": 1, "review:no_matching_product": 3,
        }
        report.write(tmp_path / "report.json")
        saved = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
        assert saved["rejection_samples"]["review:no_matching_product"]
        assert "AFAKEUSERID" not in (tmp_path / "report.json").read_text(encoding="utf-8")
        conn.close()

    def test_exact_identifier_match_only(self, catalogue_db: Path) -> None:
        conn = sqlite3.connect(catalogue_db)
        ingest(conn, AboAdapter(version="fixture"), read_jsonl(ABO))  # B0CROSS0001 exists, but in ABO
        report = run_amazon(conn)
        samples = " ".join(report.rejection_samples["review:no_matching_product"])
        for orphan in ("TST-GONE-001", "tst-snk-001", "B0CROSS0001"):
            assert orphan in samples
        abo_reviews = conn.execute("SELECT COUNT(*) FROM reviews r JOIN products p ON p.id = r.product_id "
                                   "JOIN data_sources s ON s.id = p.source_id WHERE s.name = 'abo-amazon-in'")
        assert abo_reviews.fetchone()[0] == 0
        linked = conn.execute("SELECT p.external_id, COUNT(*) FROM reviews r JOIN products p ON p.id = r.product_id "
                              "JOIN data_sources s ON s.id = r.source_id WHERE s.name = 'amazon-reviews-2023' "
                              "GROUP BY p.external_id").fetchall()
        assert dict(linked) == {"TST-SNK-001": 3, "TST-EAR-001": 1}
        conn.close()

    def test_stored_values_and_provenance(self, catalogue_db: Path) -> None:
        conn = sqlite3.connect(catalogue_db)
        run_amazon(conn)
        snk = product(conn, "TST-SNK-001")
        assert (snk["price"], snk["currency"], snk["brand"], snk["category"]) == \
            (49.99, "USD", "Fixturo", "Clothing, Shoes & Jewelry")
        attrs = json.loads(snk["attributes_json"])
        assert attrs["provenance"] == {"source": "amazon-reviews-2023", "revision": "fixture-rev", "ref": "line 1"}
        assert attrs["category_path"][-1] == "Fashion Sneakers"
        ear = product(conn, "TST-EAR-001")
        assert ear["price"] is None and ear["currency"] is None and ear["brand_id"] is None  # no 'USD' default
        assert ear["image_url"] is None
        src = conn.execute("SELECT url, license, downloaded_at, notes FROM data_sources "
                           "WHERE name = 'amazon-reviews-2023'").fetchone()
        assert "No licence stated" in src[1] and src[2] == "2026-10-21T00:00:00Z"
        assert json.loads(src[3])["files"] == [{"name": "meta.jsonl", "sha256": "aa" * 32}]
        conn.close()

    def test_no_personal_data_in_database(self, catalogue_db: Path) -> None:
        conn = sqlite3.connect(catalogue_db)
        run_amazon(conn)
        conn.commit()
        conn.close()
        assert b"AFAKEUSERID" not in Path(catalogue_db).read_bytes()

    def test_rerun_is_idempotent(self, catalogue_db: Path) -> None:
        conn = sqlite3.connect(catalogue_db)
        run_amazon(conn)
        before = conn.execute("SELECT (SELECT COUNT(*) FROM products), (SELECT COUNT(*) FROM reviews), "
                              "(SELECT COUNT(*) FROM data_sources)").fetchone()
        again = run_amazon(conn)
        assert (again.products_inserted, again.reviews_inserted) == (0, 0)
        assert (again.products_existing, again.reviews_existing) == (6, 4)
        assert conn.execute("SELECT (SELECT COUNT(*) FROM products), (SELECT COUNT(*) FROM reviews), "
                            "(SELECT COUNT(*) FROM data_sources)").fetchone() == before
        conn.close()

    def test_per_product_cap_is_deterministic(self, catalogue_db: Path) -> None:
        conn = sqlite3.connect(catalogue_db)
        report = run_amazon(conn, max_reviews_per_product=2)
        assert report.rejections["review:over_per_product_cap"] == 1
        kept = [r[0] for r in conn.execute(
            "SELECT r.title FROM reviews r JOIN products p ON p.id = r.product_id "
            "WHERE p.external_id = 'TST-SNK-001' ORDER BY r.title")]
        assert kept == ["Comfortable", "Good value"]  # verified first, then helpful votes, then newest
        conn.close()

    def test_failure_rolls_back_everything(self, catalogue_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = sqlite3.connect(catalogue_db)
        before = conn.execute("SELECT (SELECT COUNT(*) FROM products), (SELECT COUNT(*) FROM reviews), "
                              "(SELECT COUNT(*) FROM data_sources)").fetchone()
        import src.data.adapters.pipeline as pipe

        def boom(*_a, **_k):
            raise RuntimeError("simulated failure mid-ingest")
        monkeypatch.setattr(pipe, "validate_review", boom)
        with pytest.raises(RuntimeError, match="simulated"):
            run_amazon(conn)
        assert conn.execute("SELECT (SELECT COUNT(*) FROM products), (SELECT COUNT(*) FROM reviews), "
                            "(SELECT COUNT(*) FROM data_sources)").fetchone() == before
        conn.close()

    def test_existing_fixture_catalogue_untouched(self, catalogue_db: Path) -> None:
        conn = sqlite3.connect(catalogue_db)
        before = conn.execute("SELECT * FROM products WHERE source_id = 1 ORDER BY id").fetchall()
        run_amazon(conn)
        ingest(conn, AboAdapter(version="fixture"), read_jsonl(ABO))
        assert conn.execute("SELECT * FROM products WHERE source_id = 1 ORDER BY id").fetchall() == before
        conn.close()
