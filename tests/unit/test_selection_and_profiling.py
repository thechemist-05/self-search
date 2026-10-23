"""Selection rules, quota allocation, download verification and profiling (fixtures only)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import fetch_amazon2023 as fetch
from scripts import profile_amazon2023 as prof
from src.config.settings import PROJECT_ROOT
from src.data.adapters.amazon_reviews_2023 import AmazonReviews2023Adapter
from src.data.selection import SelectionConfigError, allocate, has_phrase, load_selection_config

CONFIG = PROJECT_ROOT / "config" / "selection_amazon2023.toml"
FIX = PROJECT_ROOT / "tests" / "fixtures" / "adapters"


class TestSelectionConfig:
    def test_project_config_loads(self) -> None:
        cfg = load_selection_config(CONFIG)
        assert (cfg.min_qualifying_reviews, cfg.min_review_chars, cfg.max_reviews_per_product) == (3, 20, 8)
        assert cfg.categories["fashion"].quota == 12000 and cfg.categories["electronics"].quota == 8000

    @pytest.mark.parametrize("title,expected", [
        ("Men's Street Sneakers", "sneakers"),
        ("Leather Ankle Boots for Women", "boots"),
        ("Bootcut Jeans", "jeans_trousers"),          # 'boot cut' excluded from boots
        ("Shoe Horn Long Handle", None),              # accessory excluded
        ("Classic Cotton T-Shirt", "tshirts"),
        ("College Backpack 30L", "backpacks"),
        ("Backpack Charm Keychain", None),
        ("Ceramic Vase", None),
    ])
    def test_fashion_types(self, title: str, expected) -> None:
        assert load_selection_config(CONFIG).categories["fashion"].type_for(title) == expected

    @pytest.mark.parametrize("title,expected", [
        ("Wireless Earbuds with Charging Case", "earbuds_headphones"),
        ("Replacement Ear Tips for Earbuds", None),
        ("Gaming Mouse Pad XL", None),
        ("Wireless Mouse 2.4G", "keyboards_mice"),
        ("Watch Band for Smart Watch", None),
        ("20000mAh Power Bank", "chargers_power"),
    ])
    def test_electronics_types(self, title: str, expected) -> None:
        assert load_selection_config(CONFIG).categories["electronics"].type_for(title) == expected

    def test_whole_words_only(self) -> None:
        assert has_phrase("Classic tee", "tee") and not has_phrase("Guarantee included", "tee")

    def test_invalid_config(self, tmp_path: Path) -> None:
        bad = tmp_path / "s.toml"
        bad.write_text('version = "x"\nmin_qualifying_reviews = 0\nmin_review_chars = 20\n'
                       'max_reviews_per_product = 8\n', encoding="utf-8")
        with pytest.raises(SelectionConfigError, match="positive integer"):
            load_selection_config(bad)


class TestAllocate:
    def test_equal_shares_when_all_have_enough(self) -> None:
        assert allocate(9, {"a": 100, "b": 100, "c": 100}) == {"a": 3, "b": 3, "c": 3}

    def test_unused_share_passes_on(self) -> None:
        assert allocate(12, {"a": 1, "b": 100, "c": 100}) == {"a": 1, "b": 6, "c": 5}

    def test_never_exceeds_capacity_or_quota(self) -> None:
        alloc = allocate(1000, {"a": 5, "b": 7, "c": 0})
        assert alloc == {"a": 5, "b": 7, "c": 0}
        assert sum(allocate(10, {"a": 4, "b": 4, "c": 4}).values()) == 10

    def test_deterministic(self) -> None:
        caps = {"x": 13, "y": 2, "z": 40, "w": 9}
        assert allocate(31, caps) == allocate(31, dict(reversed(caps.items())))


class TestFetchVerification:
    @pytest.fixture
    def fake(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        payload = b'{"a": 1}\n' * 1000
        monkeypatch.setattr(fetch, "ROOT", tmp_path)
        monkeypatch.setattr(fetch, "FILES", {"raw/x.jsonl": (len(payload), hashlib.sha256(payload).hexdigest())})

        class Resp:
            def __init__(self, data, status=200): self.data, self.status_code = data, status
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def iter_content(self, n): yield from (self.data[i:i + n] for i in range(0, len(self.data), n))
        return payload, Resp

    def test_verified_file_is_renamed_and_recorded(self, fake, tmp_path, monkeypatch) -> None:
        payload, Resp = fake
        monkeypatch.setattr(fetch.requests, "get", lambda *a, **k: Resp(payload))
        fetch.fetch("raw/x.jsonl")
        assert (tmp_path / "raw/x.jsonl").read_bytes() == payload
        source = json.loads((tmp_path / "SOURCE.json").read_text(encoding="utf-8"))
        entry = source["files"]["raw/x.jsonl"]
        assert entry["verified_sha256"] == entry["published_sha256"]
        assert "No licence stated" in source["license_status"]

    def test_corrupt_download_is_never_given_the_final_name(self, fake, tmp_path, monkeypatch) -> None:
        payload, Resp = fake
        monkeypatch.setattr(fetch.requests, "get", lambda *a, **k: Resp(payload[:-1] + b"X"))
        with pytest.raises(SystemExit, match="failed verification"):
            fetch.fetch("raw/x.jsonl")
        assert not (tmp_path / "raw/x.jsonl").exists() and (tmp_path / "raw/x.jsonl.bad").exists()

    def test_resume_uses_range_request(self, fake, tmp_path, monkeypatch) -> None:
        payload, Resp = fake
        (tmp_path / "raw").mkdir()
        (tmp_path / "raw/x.jsonl.part").write_bytes(payload[:100])
        seen = {}

        def get(url, headers=None, **k):
            seen.update(headers or {})
            return Resp(payload[100:], 206)
        monkeypatch.setattr(fetch.requests, "get", get)
        fetch.fetch("raw/x.jsonl")
        assert seen == {"Range": "bytes=100-"} and (tmp_path / "raw/x.jsonl").read_bytes() == payload

    def test_stops_when_disk_is_short(self, fake, monkeypatch) -> None:
        monkeypatch.setattr(fetch.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
        with pytest.raises(SystemExit, match="STOP"):
            fetch.fetch("raw/x.jsonl")

    def test_profiler_refuses_unverified_files(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(prof, "ROOT", tmp_path)
        (tmp_path / "SOURCE.json").write_text(json.dumps({"files": {}}), encoding="utf-8")
        with pytest.raises(SystemExit, match="REFUSED"):
            prof.verified_path("raw/meta_categories/meta_Amazon_Fashion.jsonl")


class TestProfiler:
    def test_counts_on_fixtures(self) -> None:
        rules = load_selection_config(CONFIG).categories["fashion"]
        adapter = AmazonReviews2023Adapter(revision="fixture")
        meta, candidates, all_ids = prof.profile_meta(FIX / "amazon2023_meta.jsonl", rules, adapter)
        assert meta["lines"] == 11 and meta["malformed"] == 1
        assert meta["accepted"] == 6 and meta["duplicate_parent_asin"] == 1
        assert meta["currency"] == {"USD": 1, "none": 5}
        assert meta["coverage"]["source_price"] == 1 and meta["inr_markers_in_price_or_title"] == 0
        assert {k: c.type_key for k, c in candidates.items()} == {
            "TST-SNK-001": "sneakers", "TST-SNK-002": "sneakers", "TST-BAG-001": "backpacks",
            "TST-ZER-001": "boots"}  # earbuds are electronics; the vase has no type
        reviews = prof.profile_reviews(FIX / "amazon2023_reviews.jsonl", candidates, all_ids, adapter, 20)
        assert reviews["lines"] == 12
        assert reviews["no_matching_product"] == 3  # unknown id, wrong case, ABO-only id
        snk = candidates["TST-SNK-001"]
        assert snk.reviews_matched == 4 and snk.qualifying == 3 and snk.duplicates == 1
        assert snk.qualifying_verified == 2


CONFIG_V2 = PROJECT_ROOT / "config" / "selection_amazon2023_v2.toml"
V1_SHA256 = "3af8a74e11cfb5b53c79d8bd4ed431683664250fd51a5a6ac7aa889e1ec54abb"  # profiled in phase 3


class TestSelectionV2:
    def fashion(self):
        return load_selection_config(CONFIG_V2).categories["fashion"]

    def test_version_and_quota(self) -> None:
        cfg = load_selection_config(CONFIG_V2)
        assert cfg.version == "selection-v2" and set(cfg.categories) == {"fashion"}
        assert cfg.categories["fashion"].quota == 20000
        assert (cfg.min_qualifying_reviews, cfg.min_review_chars, cfg.max_reviews_per_product) == (3, 20, 8)

    def test_v1_is_unchanged(self) -> None:
        assert hashlib.sha256(CONFIG.read_bytes()).hexdigest() == V1_SHA256
        assert load_selection_config(CONFIG).version == "selection-v1"

    # Titles found misclassified by selection-v1 in the phase 3 spot check / word scan.
    @pytest.mark.parametrize("title,v1_type", [
        ("Gold Toe Men's PowerSox Wool Blend Boot Crew Socks 2 Pack (mens 9-12.5 Black Marl)", "boots"),
        ("Hyp Cuphead and King Dice Men's Crew Socks 2 Pair Pack Shoe Size 6-12", "other_shoes"),
        ("Frozen Girls 6 pk Ankle Socks (4-6 (Shoe: 7-10))", "other_shoes"),
        ("Ear Jacket Peeking Feather Rhodium Plated Stud Fashion Earring", "jackets"),
        ("Orient Symphony IV Japanese Automatic/Handwinding Dress Watch", "dresses_kurtas"),
        ("Women's Watches Gorgeous Luxury Dress Casual Fashion Waterproof Watches", "dresses_kurtas"),
        ("Itslife Women's Rfid Blocking Small Compact Bifold Leather Pocket Wallet Ladies Mini Purse", "handbags"),
        ("SHANSHUI Credit Card Holder, RFID Blocking Genuine Leather Coin Purse Wallet", "handbags"),
        ("Packing Cubes 9 PCS Lightweight Compression Travel Luggage Organizer include Shoe Organizer", "other_shoes"),
        ("Miscly Round Shoe laces [3 Pairs] for Shoes, Sneakers & Boots", "sneakers"),
        ("Shoe Charms Fits for Clog Sandals Decoration, 14 pieces of butterfly", "sandals"),
        ("Lady Gift Rhinestone Travel Crystal Keychain Car Key Chain Purse Pendant Handbag Charm", "handbags"),
        ("Retro Lace Anklets Sexy Rhinestone Anklet Foot Sandal Beach Wedding Jewelry", "sandals"),
        ("Women Bridal Shiny Stretch Satin Dress Gloves Special Occasion Party Gloves Long", "dresses_kurtas"),
    ])
    def test_known_false_positives_are_excluded(self, title: str, v1_type: str) -> None:
        assert load_selection_config(CONFIG).categories["fashion"].type_for(title) == v1_type
        assert self.fashion().type_for(title) is None

    @pytest.mark.parametrize("title", [
        "Tshirt Dresses for Women Casual Loose Long Sleeve Swing Dress with Pockets Medium Navy Blue",
        "Simier Fariry Womens Summer Pleated Pockets Swing Beach T Shirt Dress Blue M",
        "KORSIS Women's Summer Floral Dresses T Shirt Dress Round Flower Navy Blue 3XL",
    ])
    def test_shirt_dresses_become_dresses(self, title: str) -> None:
        assert self.fashion().type_for(title) == "dresses_kurtas"

    # Genuine products that must keep their type (ambiguous words deliberately not excluded).
    @pytest.mark.parametrize("title,expected", [
        ("GLOBTOUCH Boy and Girls Athletic Water Shoes Quick-Dry Slip on Aqua Sock for Beach Pool", "other_shoes"),
        ("CLUCI Handbags for Women Leather Tote Shoulder Bag Big Capacity Fashion Handbags Wallet", "handbags"),
        ("Witchcraft Punk Gothic Zip-up Hoodie | O-Ring Eyelt Long Sleeve Coat Jacket Sweatshirt", "jackets"),
        ("Women's Vintage Lace V Back Bridesmaid Party Dress with Belt", "dresses_kurtas"),
        ("OWIN Women's Retro Floral Lace Cap Sleeve Vintage Swing Bridesmaid Dress (S, White)", "dresses_kurtas"),
        ("Imily Bela Women's Short Sleeve Colorblock Lace Patch Pocket T Shirt Tees", "tshirts"),
        ("Men's Slim Fit Long Sleeve Dress Shirt", "shirts"),
        ("Clarks Men's Driftway Top Chukka Boot", "boots"),
        ("Under Armour Men's Horizon RTT Running Shoe (9 M US, Black)", "running_shoes"),
        ("Women's Lace-Up Canvas Sneakers", "sneakers"),
        ("CAMEL CROWN 30L/40L Waterproof Hiking Backpack Travel Daypack", "backpacks"),
    ])
    def test_genuine_products_keep_their_type(self, title: str, expected: str) -> None:
        assert self.fashion().type_for(title) == expected

    def test_category_exclusion_is_reported(self) -> None:
        assert self.fashion().excluded_by("Men's Crew Socks, Fits Shoe Size 6-12") == "socks"
        assert self.fashion().excluded_by("Men's Running Shoe") is None

    def test_single_category_file_loads_and_empty_file_is_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "s.toml"
        p.write_text('version = "x"\nmin_qualifying_reviews = 3\nmin_review_chars = 20\n'
                     'max_reviews_per_product = 8\n', encoding="utf-8")
        with pytest.raises(SelectionConfigError, match="no category section"):
            load_selection_config(p)
