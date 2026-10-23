"""Selection-v4 helpers used by scripts/build_expanded_db.py (2026-10-23)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import build_expanded_db as build
from src.data.adapters.amazon_reviews_2023 import AmazonReviews2023Adapter
from src.data.selection import (
    TypeRule,
    allocate,
    allocate_groups,
    allocate_with_priority,
    load_selection_config,
    near_title_key,
)

V4 = Path("config/selection_amazon2023_v4_additions.toml")


@pytest.mark.parametrize("title,expected", [
    ("Banarasi Silk Saree with Blouse Piece", "saree_banarasi"),
    ("Pure Cotton Handloom Saree", "saree_cotton"),
    ("Pink Georgette Sari", "saree_chiffon_georgette"),
    ("Women Readymade Saree Blouse", None),                 # a blouse sold for sarees is not a saree
    ("Saree Shapewear Petticoat", None),
    ("Bridal Lehenga Choli with Dupatta", "lehenga_bridal"),
    ("Mirror Work Lehenga", "lehenga_mirror_work"),
    ("Girls Ghagra Choli Set", "lehenga_other"),
    ("Kundan Jhumka Earrings for Women", "earrings_jhumka"),
    ("Gold Plated Chandbali", "earrings_chandbali"),
    ("Pearl Stud Earrings", "earrings_studs"),
    ("Necklace and Earrings Set", "jewellery_sets"),        # a set is other jewellery, never earrings
    ("Gold Plated Choker Necklace", "necklaces"),
    ("Kundan Maang Tikka", "maang_tikka"),
    ("Silver Toe Ring", "rings"),
    ("Earring Backs 100 pcs", None),                        # accessories are not earrings
    ("Men's Kurta Pajama Set", "mens_kurtas"),
    ("Men's Running Shoes", "mens_footwear"),               # men's footwear is men's fashion
    ("Women's Running Shoes", "footwear_sneakers"),
    ("Punjabi Jutti for Women", "footwear_juttis"),
    ("Leather Bifold Wallet", "bags_wallets"),
    ("Polarized Sunglasses for Women", "bags_sunglasses"),
])
def test_v4_typing(title, expected) -> None:
    rules = load_selection_config(V4).categories["clothing"]
    assert rules.type_for(title) == expected


@pytest.mark.parametrize("section,title,expected", [
    ("beauty", "Matte Liquid Lipstick Long Lasting", "makeup"),
    ("beauty", "Vitamin C Face Serum", "skincare"),
    ("beauty", "Argan Hair Oil", "haircare"),
    ("beauty", "Eau de Parfum for Women", "fragrances"),
    ("beauty", "Beard Oil for Men", "grooming"),
    ("handmade", "Handmade Macrame Wall Hanging", "home_decor"),
    ("handmade", "Ceramic Coffee Mug Handmade", "home_drinkware"),
    ("handmade", "Handmade Silver Earrings", None),          # jewellery is not home and lifestyle
    ("sports", "Non Slip Yoga Mat 6mm", "sports_yoga_mats"),
    ("sports", "Adjustable Dumbbells Set", "sports_fitness_equipment"),
    ("sports", "Camping Tent 4 Person", None),
])
def test_v4_other_sections(section, title, expected) -> None:
    assert load_selection_config(V4).categories[section].type_for(title) == expected


def test_v4_allocation_matches_the_request() -> None:
    cfg = load_selection_config(V4)
    groups = {g: q for r in cfg.categories.values() for g, q in r.groups}
    assert groups == {"sarees": 8000, "lehengas": 7000, "earrings": 6000, "jewellery": 4000, "mens": 5000,
                      "footwear": 7000, "bags": 5000, "beauty": 4000, "home": 3000, "sports": 1000}
    assert sum(groups.values()) == 50000 == sum(r.quota for r in cfg.categories.values())
    assert cfg.min_qualifying_reviews == 1 and cfg.max_reviews_per_product == 8
    assert all(t.group for r in cfg.categories.values() for t in r.types)  # every type is allocated


def test_group_allocation_spreads_each_quota_and_never_borrows() -> None:
    rules = load_selection_config(V4).categories["clothing"]
    caps = {t.key: 0 for t in rules.types}
    caps.update({"saree_silk": 10000, "saree_cotton": 10000, "saree_other": 100, "earrings_jhumka": 50})
    alloc = allocate_groups(rules, caps)
    assert alloc["saree_silk"] + alloc["saree_cotton"] + alloc["saree_other"] == 8000
    assert alloc["saree_other"] == 100 and alloc["saree_silk"] == alloc["saree_cotton"] == 3950
    assert alloc["earrings_jhumka"] == 50            # a short group is not padded from another
    assert sum(v for k, v in alloc.items() if k.startswith("footwear")) == 0


def test_priority_allocation_takes_priority_types_first_then_balances() -> None:
    types = (TypeRule("sarees", ("saree",), priority=True), TypeRule("earrings", ("earring",), priority=True,
                                                                     max_products=100),
             TypeRule("a", ("a",)), TypeRule("b", ("b",)))
    alloc = allocate_with_priority(500, {"sarees": 30, "earrings": 900, "a": 1000, "b": 50}, types)
    assert alloc == {"sarees": 30, "earrings": 100, "a": 320, "b": 50}
    assert sum(alloc.values()) == 500
    plain = (TypeRule("a", ("a",)), TypeRule("b", ("b",)))
    assert allocate_with_priority(500, {"a": 1000, "b": 50}, plain) == allocate(500, {"a": 1000, "b": 50})


def test_near_duplicate_key_ignores_variant_words_but_not_sellers() -> None:
    a = near_title_key("Red Kundan Jhumka Earrings for Women - Pack of 2", "Zaveri")
    assert a == near_title_key("Green Kundan Jhumka Earrings for Women", "Zaveri")
    assert a != near_title_key("Green Kundan Jhumka Earrings for Women", "OtherSeller")
    assert near_title_key("Red Saree", "Zaveri") == ""  # too little left: never treated as a duplicate


def test_dedupe_against_base_and_within_additions() -> None:
    # entry: (type, title_key, near_key, rating_count)
    q = {"A": ("sarees", "silk saree red", "s|silk saree", 9), "B": ("sarees", "silk saree red", "s|silk saree", 9),
         "C": ("sarees", "cotton saree green", "s|cotton saree", 9), "D": ("sarees", "georgette saree", "s|georgette x", 9),
         "E": ("earrings", "jhumka kundan", "z|jhumka kundan", 9), "F": ("earrings", "", "", 9)}
    reviews = {"A": 5, "B": 7, "C": 4, "D": 6, "E": 3, "F": 3}
    keep, stats = build.dedupe(q, reviews, base_titles={"jhumka kundan"}, base_near={"s|cotton saree"})
    assert set(keep) == {"B", "D", "F"}               # B beats A (more reviews); C, E already in the base
    assert stats["identical_title_in_base"] == 1 and stats["near_duplicate_in_base"] == 1
    assert stats["identical_title_within_additions"] == 1


def test_images_are_the_listing_own_https_photos_main_first() -> None:
    rec = {"images": [
        {"variant": "PT01", "large": "https://m.media-amazon.com/images/I/b.jpg"},
        {"variant": "MAIN", "large": "https://m.media-amazon.com/images/I/a.jpg"},
        {"variant": "PT02", "large": "https://m.media-amazon.com/images/I/blank.gif"},   # placeholder
        {"variant": "PT03", "large": "http://insecure.example/c.jpg"},                   # not https
    ]}
    assert build.images_from(rec) == ["https://m.media-amazon.com/images/I/a.jpg",
                                      "https://m.media-amazon.com/images/I/b.jpg"]
    assert build.images_from({"images": []}) == [] and build.images_from({}) == []


def test_profile_additions_skips_base_products_and_low_rated_items(tmp_path: Path) -> None:
    lines = [
        {"parent_asin": "IN-BASE", "title": "Red Silk Saree", "rating_number": 50, "average_rating": 4.0},
        {"parent_asin": "NEW-SAREE", "title": "Blue Silk Saree", "rating_number": 50, "average_rating": 4.0},
        {"parent_asin": "FEW", "title": "Green Silk Saree", "rating_number": 2, "average_rating": 4.0},
        {"parent_asin": "SET", "title": "Necklace and Jhumka Earrings Set", "rating_number": 40, "average_rating": 4.0},
        {"parent_asin": "SHOE", "title": "Women's Running Shoes", "rating_number": 30, "average_rating": 4.0},
    ]
    meta = tmp_path / "meta.jsonl"
    meta.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    rules = load_selection_config(V4).categories["clothing"]
    kept, stats = build.profile_additions(meta, rules, AmazonReviews2023Adapter(revision="test"), {"IN-BASE"},
                                          min_ratings=3)
    assert set(kept) == {"NEW-SAREE", "SHOE", "SET"}
    assert kept["NEW-SAREE"][0] == "saree_silk" and kept["SHOE"][0] == "footwear_sneakers"
    assert kept["SET"][0] == "jewellery_sets"
    assert stats["duplicate_asin_in_base"] == 1 and stats["fewer_than_3_ratings"] == 1


def test_parallel_profile_equals_the_serial_pass(tmp_path: Path) -> None:
    rows = [{"parent_asin": f"P{i}", "title": t, "rating_number": 5 + i, "average_rating": 4.0}
            for i, t in enumerate(["Red Silk Saree", "Kundan Jhumka Earrings", "Women's Running Shoes",
                                   "Leather Bifold Wallet", "Gold Plated Choker Necklace", "Bridal Lehenga Choli",
                                   "Men's Cotton Kurta", "Polarized Sunglasses", "Pearl Stud Earrings"] * 7)]
    meta = tmp_path / "meta.jsonl"
    meta.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    rules = load_selection_config(V4).categories["clothing"]
    serial, _ = build.profile_additions(meta, rules, AmazonReviews2023Adapter(revision="t"), {"P3"}, 1)
    for workers in (1, 2, 3, 7, 16):          # boundaries fall mid-line and at line starts
        parallel, stats = build.profile_additions_parallel(meta, rules, "t", {"P3"}, 1, workers)
        assert parallel == serial, workers
        assert stats["accepted"] == len(rows)


def test_bounded_review_collection_keeps_exactly_what_ingest_keeps(tmp_path: Path) -> None:
    from src.data.adapters.pipeline import _review_rank

    reviews = []
    for i in range(40):
        reviews.append({"parent_asin": "A", "asin": "A", "rating": 5, "title": "t", "timestamp": 1600000000000 + i,
                        "helpful_vote": i % 7, "verified_purchase": i % 3 == 0,
                        "text": "short" if i % 5 == 0 else f"A genuinely useful review number {i} of this item."})
    reviews.append(dict(reviews[1]))                       # an exact duplicate
    reviews.append({**reviews[2], "parent_asin": "OTHER"})  # another product
    path = tmp_path / "rev.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in reviews) + "\n", encoding="utf-8")
    adapter = AmazonReviews2023Adapter(revision="t")
    kept = build.collect_reviews_bounded(path, {"A"}, adapter, keep=8, min_chars=20)
    assert len(kept) == 8 and all(r["parent_asin"] == "A" for r in kept)
    parsed = [r for r in adapter.parse_reviews(json.loads(l) for l in path.read_text().splitlines())
              if not hasattr(r, "reason") and r.product_external_id == "A" and len(r.text) >= 20]
    unique = list({r.external_id: r for r in parsed}.values())
    expected = sorted(unique, key=_review_rank)[:8]
    got = sorted(adapter.parse_reviews(kept), key=_review_rank)
    assert [r.external_id for r in got] == [r.external_id for r in expected]
