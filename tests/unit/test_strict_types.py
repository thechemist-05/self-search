"""Strict type, colour, material and brand rules (2026-10-22): adversarial titles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.retrieval.constraints import (
    brand_matches,
    colour_matches,
    detect_colours,
    detect_materials,
    detect_product_types,
    product_colours,
    product_materials,
    strict_type_match,
)


@pytest.mark.parametrize("title,ptype,ok", [
    ("Women's Black Stiletto Pointed Toe High Heels Pumps", "heels", True),
    ("Women's Chunky Heel Ankle Boots", "heels", False),             # a heeled boot is a boot
    ("Heel Grips for Loose Shoes", "heels", False),                   # accessory naming heels
    ("Women's Flat Heel Sandals", "heels", False),                    # "flat heel" = no heel
    ("Men's Running Shoes Lightweight", "running shoes", True),
    ("adidas Men's Nemeziz 19.4 FxG Football Shoe", "running shoes", False),
    ("Women's Running Sandals Athletic", "running shoes", False),
    ("Sneaker Cleaner Kit for Running Shoes", "running shoes", False),
    ("SneakERASERS Overnight Soak, Shoe and Sneaker Cleaner", "footwear", False),
    ("D&L Store Men's Shoe clouser Arch Support", "footwear", False),
    ("Women's Running Shorts with Pockets", "running shoes", False),
    ("Women's Black Leather Crossbody Handbag", "handbag", True),
    ("Women's Black Leather Ankle Boots", "handbag", False),
    ("Purse Organizer Insert for Tote", "handbag", False),
    ("Floral Short Sleeve Summer Dress", "shorts", False),
    ("Women's Denim Shorts", "shorts", True),
    ("Women's Shirt Dress Button Down", "dress", True),
    ("Men's Slim Fit Dress Shirt", "dress", False),
    ("Red Dress Shoes for Women", "dress", False),
    ("Women's Wrap Dress with Belt", "belt", False),
    ("Cap Sleeve Maxi Dress", "hat", False),
    ("Men's Baseball Cap Adjustable", "hat", True),
    ("Women's Bootcut Jeans", "boots", False),
    ("Women's Bootcut Jeans", "jeans", True),
    ("Men's Jean Jacket Denim", "jeans", False),
    # found by the returned-result evaluation (2026-10-22)
    ("H.S.D Cool Knotted Short Denim Shawl Coat Top Jacket", "shorts", False),   # "short" the adjective
    ("Season Show Girls Overalls Short Destroyed Denim Jeans", "shorts", False),
    ("Women Denim Shorts High Waisted", "shorts", True),
    ("Cuziss Webbing Ending Clip Quick Slip Keeper Buckle for Backpack", "backpack", False),  # *for* a backpack
    ("Black Laptop Backpack for Women", "backpack", True),
    ("Gel Insoles for Running Shoes", "running shoes", False),
    ("Kangaroo Joey Bag For Feeding Pumps - Kangaroo Backpack For Enteral Feeding Pump", "heels", False),
    ("Women's Shoe Bag Travel Organizer", "footwear", False),
    # found by the live end-to-end check (2026-10-22): bags named after dresses
    ("Charming Tailor Sparkling Glitter Box Clutch Evening Bag Party Purse Bling Dress Handbag", "dress", False),
    ("Kenley Travel Garment Bag Suit Dress Case Cover", "dress", False),
    ("Donalworld Women Cute Mini Dress Jeans Denim Shoulder Bags Dblue", "dress", False),
    ("EFWEL Kids Magic Family Costume Dress - Girls Halloween Outfit Clothes with Bag", "dress", True),
])
def test_strict_type_rules(title, ptype, ok) -> None:
    assert (strict_type_match(title, ptype) is not None) is ok


@pytest.mark.parametrize("query,types", [
    ("black heels", ["heels"]), ("black handbag", ["handbag"]), ("running shoes", ["running shoes"]),
    ("football boots", ["football shoes"]), ("short sleeve t-shirt", ["t-shirt"]), ("red summer dress", ["dress"]),
    ("dress shoes", ["footwear"]), ("shoulder bag", ["handbag"]),
])
def test_query_types(query, types) -> None:
    assert detect_product_types(query) == types


def test_colours_are_read_conservatively() -> None:
    assert product_colours({"title": "Red Wing Heritage Moc Toe Boots"}) == ([], None)   # a brand, not a colour
    assert product_colours({"title": "Women's Summer Maxi Dress"}) == ([], None)          # unstated = unknown
    shades, src = product_colours({"title": "Black Pumps", "attributes_json": json.dumps({"details": {"Color": "Navy Blue"}})})
    assert (shades, src) == (["navy"], "listing details")                                  # structured field wins
    assert colour_matches("blue", ["navy"]) and colour_matches("navy", ["navy"]) and not colour_matches("navy", ["blue"])
    assert colour_matches("red", ["burgundy"]) and not colour_matches("black", ["white"])
    assert detect_colours("heels, navy blue or black") == ["navy", "black"]


def test_materials_distinguish_faux_leather_and_lace_up() -> None:
    assert product_materials({"title": "PU Leather Handbag"})[0] == ["faux leather"]
    assert product_materials({"title": "Genuine Leather Wallet"})[0] == ["leather"]
    assert product_materials({"title": "Lace Up Boots"})[0] == []


def test_listing_stating_faux_leather_is_not_genuine_leather_unless_explicit() -> None:
    stuffed = {"title": "Brown Billfold Coffee Leather Wallet Men Purse Bifold PU Leather Wallet"}
    assert product_materials(stuffed)[0] == ["faux leather"]
    mixed = {"title": "Genuine Leather Wallet with PU Leather Lining"}
    assert set(product_materials(mixed)[0]) == {"leather", "faux leather"}
    detailed = {"title": "Wallet", "attributes": {"details": {"Material": "Leather, PU Leather"}}}
    assert product_materials(detailed) == (["faux leather"], "listing details")
    assert product_materials({"title": "Top Grain Leather Belt"})[0] == ["leather"]
    assert detect_materials("vegan leather jacket") == ["faux leather"]


def test_brand_needs_whole_words() -> None:
    p = {"title": "Nikeland Kids Sneakers", "attributes_json": json.dumps({"store": "Nikeland"})}
    assert brand_matches(p, "nike") is None
    assert brand_matches({"title": "Nike Women's Running Shoes"}, "nike") == "title"
    assert brand_matches({"title": "x", "attributes_json": json.dumps({"store": "NIKE"})}, "Nike") == "store"
    assert brand_matches({"title": "x", "brand_name": "Sony Audio"}, "sony") == "brand"


def test_selection_v3_dedupes_identical_titles_only(tmp_path: Path) -> None:
    from scripts.profile_amazon2023 import Candidate, select_products
    from src.data.selection import load_selection_config, title_key

    cfg = load_selection_config(Path("config/selection_amazon2023_v3.toml"))
    rules = cfg.categories["fashion"]
    c = {
        "A": Candidate("handbags", title_key("Womens Genuine Leather Clutch Handbags [6 Card Slots]"), qualifying=5),
        "B": Candidate("handbags", title_key("womens genuine leather clutch handbags (6 card slots)"), qualifying=7),
        "C": Candidate("handbags", title_key("Womens Genuine Leather Clutch Handbags Red"), qualifying=4),
    }
    qualifying, capacity, _, selected = select_products(c, cfg, rules)
    assert set(qualifying) == {"B", "C"}           # A and B are the same listing; B has more reviews
    assert capacity["handbags"] == 2
    v2 = load_selection_config(Path("config/selection_amazon2023_v2.toml"))
    assert set(select_products(c, v2, v2.categories["fashion"])[0]) == {"A", "B", "C"}  # v2 unchanged


@pytest.mark.parametrize("query,types,colours,materials,occasions", [
    ("red silk saree under ₹3,000", ["saree"], ["red"], ["silk"], []),
    ("bridal jhumkas under ₹1,500", ["earrings"], [], [], ["bridal"]),
    ("lehnga for my wedding", ["lehenga"], [], [], ["wedding"]),
    ("festive lehenga choli", ["lehenga"], [], [], ["festive"]),
    ("oxidised silver jhumki", ["earrings"], ["silver"], ["oxidised silver"], []),
    ("gold plated chandbalis", ["earrings"], ["gold"], ["gold plated"], []),
    ("sari", ["saree"], [], [], []),
    ("a shoe for everyday use", ["footwear"], [], [], []),   # "everyday" is not a hard occasion
])
def test_new_category_query_understanding(query, types, colours, materials, occasions) -> None:
    from src.retrieval.constraints import detect_occasions

    assert (detect_product_types(query), detect_colours(query), detect_materials(query),
            detect_occasions(query)) == (types, colours, materials, occasions)


@pytest.mark.parametrize("title,ptype,ok", [
    ("Banarasi Silk Saree with Blouse Piece", "saree", True),
    ("Readymade Saree Blouse for Women", "saree", False),          # a blouse sold for sarees
    ("Saree Shapewear Petticoat", "saree", False),
    ("Embroidered Potli Bag for Saree", "saree", False),
    ("Ready to Wear Lehenga Saree", "saree", False),              # ambiguous: neither type
    ("Ready to Wear Lehenga Saree", "lehenga", False),
    ("Bridal Lehenga Choli with Dupatta", "lehenga", True),
    ("Girls Ghagra Choli Set", "lehenga", True),
    ("Lehenga Can Can Petticoat", "lehenga", False),
    ("Kundan Jhumka Earrings Gold Plated", "earrings", True),
    ("Meenakari Chandbali", "earrings", True),
    ("Kundan Necklace with Jhumka Earrings Set", "earrings", False),  # a set is not earrings
    ("Earring Backs Replacement 100 Pieces", "earrings", False),
    ("Gold Plated Choker Necklace", "earrings", False),
])
def test_new_category_strict_types(title, ptype, ok) -> None:
    assert (strict_type_match(title, ptype) is not None) is ok


def test_occasions_are_read_from_the_listing_only() -> None:
    from src.retrieval.constraints import product_occasions

    assert product_occasions({"title": "Bridal Kundan Jhumka Earrings"}) == (["bridal", "wedding"], "title")
    assert product_occasions({"title": "Kundan Jhumka", "description": "Perfect for Diwali"}) == (["festive"], "description")
    assert product_occasions({"title": "Kundan Jhumka", "attributes_json": json.dumps({"details": {"Occasion": "Party"}})}) \
        == (["party"], "listing details")
    assert product_occasions({"title": "Kundan Jhumka"}) == ([], None)   # unstated = unknown, never assumed
    assert product_occasions({"title": "Third Party Cable"}) == ([], None)
