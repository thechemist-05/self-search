"""
Hard-constraint helpers for retrieval: product types, currencies, term matching.

Pure functions, no database access. Used by ``MetadataFilter`` to enforce
product-type and exclusion constraints against product ``title`` and
``description`` text, and by the query parser to detect them.

Product types come from a small, curated synonym list (``PRODUCT_TYPES``), used
to *detect* what a query asks for. How a *product* qualifies depends on the type:

* Fashion types (``STRICT_TYPES``) are strict and title-only: the title must state
  the type ("running" + a shoe noun for running shoes), must not state a conflicting
  type (football boots, sandals), and accessories that merely mention the type
  (shoe cleaners, shoelaces, bag charms) are vetoed. Similar words never qualify a
  product on their own.
* Other types (the original electronics/outdoor list) match a synonym in the title
  or description, as before.

Colour and material (``COLOURS``, ``MATERIALS``) are read conservatively: from the
source's structured ``details`` (Color / Material) when present, else only when the
title states them. A product whose colour is not stated is *unknown*, never assumed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

# Canonical product type -> synonym phrases (singular, lower case).
# A product matches a type if any phrase appears as whole words in its title
# or description. Judgement calls are documented in docs/rag-design.md.
PRODUCT_TYPES: dict[str, tuple[str, ...]] = {
    # ---- Fashion (strict title rules in STRICT_TYPES) ----
    "footwear": ("shoe", "footwear", "dress shoe"),
    "sneakers": ("sneaker", "trainer", "tennis shoe", "athletic shoe", "gym shoe"),
    "running shoes": ("running shoe", "running sneaker", "running trainer", "jogging shoe", "trail running shoe"),
    "football shoes": ("football shoe", "football boot", "soccer shoe", "soccer cleat", "cleat", "football cleat"),
    "boots": ("boot", "bootie", "ankle boot", "chelsea boot", "hiking boot", "snow boot", "rain boot", "work boot"),
    "sandals": ("sandal", "flip flop", "slide sandal"),
    "heels": ("heel", "high heel", "pump", "stiletto", "kitten heel"),
    "flats": ("flat", "ballet flat"),
    "slippers": ("slipper",),
    "loafers": ("loafer", "moccasin"),
    "bag": ("bag",),
    "handbag": ("handbag", "purse", "tote", "tote bag", "shoulder bag", "crossbody", "crossbody bag",
                "clutch", "satchel", "hobo bag"),
    "backpack": ("backpack", "rucksack", "daypack"),
    "wallet": ("wallet", "card holder"),
    "dress": ("dress", "gown", "sundress", "maxi dress", "midi dress", "mini dress"),
    "skirt": ("skirt",),
    "jeans": ("jean",),
    "trousers": ("trouser", "pant", "chino", "jogger", "sweatpant"),
    "shorts": ("short",),
    "t-shirt": ("t shirt", "tshirt", "tee"),
    "shirt": ("shirt", "blouse"),
    "hoodie": ("hoodie", "sweatshirt"),
    "sweater": ("sweater", "cardigan", "pullover", "jumper"),
    "leggings": ("legging",),
    "swimwear": ("swimsuit", "bikini", "swimwear", "swim trunk", "bathing suit", "tankini"),
    "hat": ("hat", "cap", "beanie", "fedora"),
    "belt": ("belt",),
    "sock": ("sock",),
    # ---- Indian ethnic wear and earrings (2026-10-23; strict title rules in STRICT_TYPES) ----
    "saree": ("saree", "sari", "saaree"),
    "lehenga": ("lehenga", "lehnga", "lengha", "lehanga", "lehenga choli", "ghagra", "ghagra choli",
                "chaniya choli"),
    "earrings": ("earring", "jhumka", "jhumki", "chandbali", "ear stud", "stud earring", "hoop earring",
                 "drop earring", "dangle earring", "ear cuff", "huggie"),
    # ---- Expanded catalogue buckets (2026-10-23; strict title rules in STRICT_TYPES) ----
    "necklace": ("necklace", "choker", "pendant", "mangalsutra"),
    "bangle": ("bangle", "kada"),
    "bracelet": ("bracelet",),
    "ring": ("finger ring", "toe ring", "ring"),
    "anklet": ("anklet", "payal"),
    "maang tikka": ("maang tikka", "mang tikka", "tikka", "matha patti"),
    "jewellery set": ("jewellery set", "jewelry set", "necklace set", "bridal set"),
    "sunglasses": ("sunglass", "shade"),
    "juttis": ("jutti", "mojari", "kolhapuri"),
    "kurta": ("kurta", "kurta pajama", "sherwani", "pathani"),
    "lipstick": ("lipstick", "lip gloss", "lip colour", "lip color"),
    "perfume": ("perfume", "fragrance", "eau de parfum", "eau de toilette", "attar", "body mist", "cologne"),
    "shampoo": ("shampoo",),
    "face serum": ("face serum", "facial serum", "serum"),
    "moisturizer": ("moisturizer", "moisturiser", "face cream", "night cream"),
    "trimmer": ("trimmer", "beard trimmer"),
    "bedsheet": ("bedsheet", "bed sheet", "bedspread", "duvet cover"),
    "cushion cover": ("cushion cover", "cushion", "pillow cover", "throw pillow"),
    "lamp": ("lamp", "table lamp", "lantern"),
    "mug": ("mug", "coffee mug", "tea cup"),
    "vase": ("vase",),
    "dumbbell": ("dumbbell", "kettlebell"),
    # ---- Original list (non-strict: title or description) ----
    "headphones": ("headphone", "headset", "earphone", "earbud"),  # earbuds are a kind of headphones
    "earbuds": ("earbud", "earphone"),
    "speaker": ("speaker", "soundbar"),
    "security camera": ("security camera",),
    "webcam": ("webcam",),
    "camera": ("camera",),
    "cable": ("cable", "cord"),
    "power bank": ("power bank", "powerbank", "portable charger"),
    "charger": ("charger",),
    "mouse": ("mouse",),
    "keyboard": ("keyboard",),
    "smartwatch": ("smartwatch", "smart watch", "fitness tracker", "watch"),
    "laptop": ("laptop", "notebook"),
    "laptop stand": ("laptop stand",),
    "yoga mat": ("yoga mat", "mat"),
    "hammock": ("hammock",),
    "water bottle": ("water bottle", "bottle", "flask"),
    "gloves": ("glove",),
    "jump rope": ("jump rope", "skipping rope"),
    "foam roller": ("foam roller",),
    "resistance bands": ("resistance band",),
    "tent": ("tent",),
    "jacket": ("jacket", "coat", "blazer", "parka", "windbreaker"),
    "tablet": ("tablet",),
    "phone": ("phone", "smartphone"),
}

@dataclass(frozen=True)
class TypeSpec:
    """Strict, title-only rule for one product type (phrases are singular, lower case).

    The title qualifies if, after removing ``masks`` (phrases that look like the type but
    are not, e.g. "short sleeve" for shorts), every group in ``all_of`` has a phrase
    present, and no phrase in ``conflicts`` (a different kind of product) is present.
    """

    all_of: tuple[tuple[str, ...], ...]
    conflicts: tuple[str, ...] = ()
    masks: tuple[str, ...] = ()
    # Match the title's words as written (no singularising): "shorts" the garment vs the
    # adjective "short" ("Short Denim Jacket"), which singularising would conflate.
    literal: bool = False


# Accessories and care products whose titles name shoes or bags without being them.
# "Buckle for Backpack", "Insoles for Shoes": an item *for* the type is not the type.
_FOR_ITEM = ("for shoe", "for sneaker", "for boot", "for heel", "for sandal", "for backpack", "for handbag",
             "for purse", "for bag", "for dress", "for jean", "for belt", "for hat")
# A bag (or a medical feeding-pump bag) whose title names shoes, heels or pumps is not footwear.
_SHOE_ACCESSORY = _FOR_ITEM + ("bag", "backpack", "handbag", "purse", "tote", "feeding", "cleaner", "cleaning", "eraser", "polish", "deodorizer", "deodorant", "stretcher",
                   "insole", "shoelace", "shoe lace", "lace lock", "clouser", "charm", "keychain", "key chain",
                   "organizer", "shoe tree", "shoe horn", "heel grip", "heel pad", "heel protector", "heel cushion",
                   "shoe clip", "shoe cover", "shoe rack", "shoe bag", "shoe box", "protector spray", "repair")
_BAG_ACCESSORY = _FOR_ITEM + ("buckle", "webbing", "clip", "charm", "keychain", "key chain", "organizer", "purse insert", "bag insert", "purse strap",
                  "handbag strap", "replacement strap", "purse hook", "purse hanger", "bag tag", "shoe bag",
                  "dust bag", "gift bag", "laundry bag", "storage bag")
_FOOTWEAR_NOUNS = ("shoe", "sneaker", "boot", "bootie", "sandal", "slipper", "loafer", "heel", "pump", "clog",
                   "mule", "cleat", "flip flop", "espadrille", "moccasin", "footwear")
_CLOTHING_NOUNS = ("dress", "skirt", "pant", "trouser", "jean", "legging", "short", "shirt", "t shirt", "blouse",
                   "jacket", "coat", "hoodie", "sweater", "sweatshirt", "romper", "jumpsuit", "swimsuit")

# Jewellery that is not earrings, earring accessories, and sets: an earring search returns
# earrings only (a matching set is a different product).
_NOT_EARRINGS = ("necklace", "pendant", "bracelet", "anklet", "bangle", "ring set", "jewelry set", "jewellery set",
                 "maang tikka", "tikka", "nose ring", "nose pin", "brooch", "earring back", "earring backing",
                 "earring holder", "earring organizer", "earring stand", "earring display", "earring card",
                 "earring hook", "earring finding", "ear wire", "earring converter", "earring lifter", "earring box",
                 "for earring", "earring post", "jewelry making", "jewellery making", "earring kit", "doll")
_ETHNIC_ACCESSORY = ("handbag", "purse", "clutch", "potli", "earring", "necklace", "doll", "brooch", "petticoat",
                     "shapewear", "storage", "organizer", "hanger", "cover", "pin", "safety pin")

STRICT_TYPES: dict[str, TypeSpec] = {
    "saree": TypeSpec((("saree", "sari", "saaree"),),
                      conflicts=_FOOTWEAR_NOUNS + _ETHNIC_ACCESSORY + ("lehenga", "kurta", "kurti"),
                      masks=("saree blouse", "sari blouse", "saree petticoat", "saree shapewear", "saree pin",
                             "saree cover", "saree bag", "saree brooch", "for saree", "for sari", "saree storage")),
    "lehenga": TypeSpec((("lehenga", "lehnga", "lengha", "lehanga", "ghagra", "chaniya choli"),),
                        conflicts=_FOOTWEAR_NOUNS + _ETHNIC_ACCESSORY + ("saree", "sari", "can can"),
                        masks=("for lehenga", "lehenga cover", "lehenga bag", "lehenga petticoat")),
    "earrings": TypeSpec((("earring", "jhumka", "jhumki", "chandbali", "ear stud", "ear cuff", "huggie"),),
                         conflicts=_NOT_EARRINGS + _CLOTHING_NOUNS + _FOOTWEAR_NOUNS),
    # Other jewellery: never earrings, sets (except the set type) or accessories for jewellery.
    "necklace": TypeSpec((("necklace", "choker", "pendant", "mangalsutra"),),
                         conflicts=("earring", "jhumka", "jhumki", "set", "necklace holder", "necklace stand",
                                    "jewelry box", "jewellery box", "chain extender", "dog", "keychain")),
    "bangle": TypeSpec((("bangle", "kada"),), conflicts=("earring", "necklace", "set", "bangle box", "holder")),
    "bracelet": TypeSpec((("bracelet",),), conflicts=("earring", "necklace", "set", "watch", "holder", "charm only")),
    "ring": TypeSpec((("ring",),), conflicts=("earring", "nose ring", "key ring", "keyring", "o ring", "d ring",
                                              "split ring", "ring light", "ring sizer", "napkin ring", "ring holder",
                                              "ring box", "boxing ring", "set"),
                     masks=("nose ring", "o ring", "d ring", "key ring", "split ring", "ring light", "napkin ring")),
    "anklet": TypeSpec((("anklet", "payal"),), conflicts=("earring", "necklace", "set")),
    "maang tikka": TypeSpec((("maang tikka", "mang tikka", "tikka", "matha patti"),), conflicts=("earring set",)),
    "jewellery set": TypeSpec((("set",), ("necklace", "choker", "bangle", "jewelry", "jewellery", "maang tikka")),
                              conflicts=("box", "organizer", "holder", "making", "bead")),
    "sunglasses": TypeSpec((("sunglass", "shade"),),
                           conflicts=("case", "strap", "holder", "lens replacement", "lamp", "window shade")),
    "juttis": TypeSpec((("jutti", "mojari", "kolhapuri"),), conflicts=_SHOE_ACCESSORY),
    "kurta": TypeSpec((("kurta", "sherwani", "pathani"),), conflicts=("kurti", "saree", "lehenga") + _FOOTWEAR_NOUNS),
    # Beauty, home and sports: products only, not their accessories.
    "lipstick": TypeSpec((("lipstick", "lip gloss", "lip colour", "lip color"),),
                         conflicts=("holder", "organizer", "case", "mould", "mold")),
    "perfume": TypeSpec((("perfume", "fragrance", "eau de parfum", "eau de toilette", "attar", "body mist", "cologne"),),
                        conflicts=("empty bottle", "atomizer", "fragrance free", "fragrance-free", "oil diffuser",
                                   "candle", "shampoo", "conditioner", "lotion", "deodorant")),
    "shampoo": TypeSpec((("shampoo",),), conflicts=("dry shampoo brush", "shampoo bottle", "dispenser", "dog", "pet")),
    "face serum": TypeSpec((("serum",),), conflicts=("hair serum", "hair")),
    "moisturizer": TypeSpec((("moisturizer", "moisturiser", "face cream", "night cream"),), conflicts=("hair",)),
    "trimmer": TypeSpec((("trimmer",),), conflicts=("hedge", "grass", "string", "replacement blade", "nose hair only")),
    "bedsheet": TypeSpec((("bedsheet", "bed sheet", "bedspread", "duvet cover"),), conflicts=("doll", "holder")),
    "cushion cover": TypeSpec((("cushion", "pillow cover", "throw pillow"),),
                              conflicts=("seat cushion for car", "shoe", "insole", "heel cushion", "pet bed")),
    "lamp": TypeSpec((("lamp", "lantern"),), conflicts=("lamp shade only", "bulb", "replacement")),
    "mug": TypeSpec((("mug",),), conflicts=("mug rack", "mug holder", "mug tree", "mug warmer")),
    "vase": TypeSpec((("vase",),), conflicts=("vase filler",)),
    "dumbbell": TypeSpec((("dumbbell", "kettlebell"),), conflicts=("rack", "stand", "holder")),
    # A bath, table or door mat is not a yoga mat (the old non-strict rule matched any "mat").
    "yoga mat": TypeSpec((("yoga mat", "exercise mat", "fitness mat", "pilates mat", "gym mat"),),
                         conflicts=("strap", "bag", "cleaner", "spray", "towel only")),
    "footwear": TypeSpec((_FOOTWEAR_NOUNS + ("trainer", "oxford"),),
                         conflicts=_SHOE_ACCESSORY + ("shirt", "pant", "legging", "sweater", "hoodie", "sock"),
                         masks=("waist trainer", "boot cut", "bootcut", "oxford shirt", "heel grip", "heel pad")),
    "sneakers": TypeSpec((("sneaker", "trainer", "tennis shoe", "athletic shoe", "running shoe", "walking shoe",
                           "gym shoe", "sport shoe", "skate shoe", "running sneaker"),),
                         conflicts=_SHOE_ACCESSORY + ("sandal", "slipper", "cleat", "football", "soccer", "sock"),
                         masks=("waist trainer",)),
    "running shoes": TypeSpec((("running", "jogging"), ("shoe", "sneaker", "trainer")),
                              conflicts=_SHOE_ACCESSORY + ("football", "soccer", "cleat", "sandal", "slipper",
                                                           "boot", "heel", "basketball", "baseball", "golf",
                                                           "sock", "short", "legging", "pant", "shirt"),
                              masks=("waist trainer",)),
    "football shoes": TypeSpec((("football", "soccer", "cleat"), ("shoe", "boot", "cleat", "sneaker")),
                               conflicts=_SHOE_ACCESSORY + ("sock", "jersey", "short", "shirt")),
    "boots": TypeSpec((("boot", "bootie"),),
                      conflicts=_SHOE_ACCESSORY + ("cleat", "football", "soccer", "sock"),
                      masks=("boot cut", "bootcut", "boot sock", "boot cuff", "boot tray", "boot shaper")),
    "sandals": TypeSpec((("sandal", "flip flop", "slide", "espadrille"),), conflicts=_SHOE_ACCESSORY + ("sock",)),
    "heels": TypeSpec((("heel", "pump", "stiletto"),),
                      conflicts=_SHOE_ACCESSORY + ("boot", "bootie", "sneaker", "slipper", "sock"),
                      masks=("heel grip", "heel pad", "heel protector", "heel cap", "heel cushion", "heel counter",
                             "heel cup", "flat heel", "no heel", "heel strap")),
    "flats": TypeSpec((("flat",),),
                      conflicts=_SHOE_ACCESSORY + ("heel", "boot", "sneaker", "pant", "short", "sock", "iron"),
                      masks=("flat cap", "flat front", "flat heel", "lay flat", "flat iron")),
    "slippers": TypeSpec((("slipper",),), conflicts=_SHOE_ACCESSORY + ("sock",)),
    "loafers": TypeSpec((("loafer", "moccasin"),), conflicts=_SHOE_ACCESSORY),
    "bag": TypeSpec((("bag", "handbag", "purse", "tote", "backpack", "rucksack", "clutch", "satchel",
                      "crossbody", "wallet"),),
                    conflicts=_BAG_ACCESSORY + _FOOTWEAR_NOUNS, masks=("shoe bag", "dust bag", "gift bag")),
    "handbag": TypeSpec((("handbag", "purse", "tote", "shoulder bag", "crossbody", "clutch", "satchel",
                          "hobo bag", "top handle bag"),),
                        conflicts=_BAG_ACCESSORY + _FOOTWEAR_NOUNS + ("wallet", "backpack"),
                        masks=("clutch purse wallet",)),
    "backpack": TypeSpec((("backpack", "rucksack", "daypack"),),
                         conflicts=_BAG_ACCESSORY + ("backpack strap", "backpack cover", "pin")),
    "wallet": TypeSpec((("wallet", "card holder", "billfold", "cardholder"),),
                       conflicts=_BAG_ACCESSORY + ("phone case",)),
    "dress": TypeSpec((("dress", "gown", "sundress"),),
                      # Bags "for" or named after dresses are bags; a dress sold "with bag" stays a dress.
                      conflicts=_FOOTWEAR_NOUNS + ("pant", "slack", "legging", "sock", "hat", "glove",
                                                   "watch", "costume accessory", "handbag", "purse", "clutch",
                                                   "tote", "backpack", "garment bag", "shoulder bag",
                                                   "evening bag"),
                      masks=("dress shoe", "dress shirt", "dress sock", "dress pant", "dress slack", "dress boot",
                             "dress sandal", "dress heel", "dress hat", "dress glove", "dress belt", "dress watch",
                             "dress up", "dress form")),
    "skirt": TypeSpec((("skirt", "skort"),), conflicts=("dress", "swimsuit", "hanger"),
                      masks=("skirt hanger", "bed skirt", "tree skirt", "swim skirt")),
    "jeans": TypeSpec((("jean",),), conflicts=("jacket", "short", "skirt", "shirt", "dress", "vest")),
    "trousers": TypeSpec((("trouser", "pant", "chino", "jogger", "slack", "sweatpant"),),
                         conflicts=("short", "legging", "dress", "pantie", "panty", "sock") + _FOOTWEAR_NOUNS,
                         masks=("dress pant",)),
    "shorts": TypeSpec((("shorts",),), literal=True,
                       conflicts=("dress", "skirt", "boot", "boots", "sock", "socks", "leggings", "pants",
                                  "jacket", "jackets", "coat", "blouse", "overalls", "for shorts")),
    "t-shirt": TypeSpec((("t shirt", "tshirt", "tee"),), conflicts=("dress", "short", "pant")),
    "shirt": TypeSpec((("shirt", "blouse"),), conflicts=("dress", "short", "pant", "skirt"),
                      masks=("t shirt", "tshirt", "shirt dress")),
    "jacket": TypeSpec((("jacket", "coat", "blazer", "parka", "windbreaker"),),
                       conflicts=("dress", "pant", "short", "skirt", "hanger"),
                       masks=("ear jacket", "coat hanger", "top coat")),
    "hoodie": TypeSpec((("hoodie", "sweatshirt"),), conflicts=("dress", "pant", "short")),
    "sweater": TypeSpec((("sweater", "cardigan", "pullover", "jumper"),), conflicts=("dress", "pant", "short"),
                        masks=("sweater dress",)),
    "leggings": TypeSpec((("legging",),), conflicts=("short", "sock", "dress")),
    "swimwear": TypeSpec((("swimsuit", "bikini", "swimwear", "swim trunk", "bathing suit", "tankini"),),
                         conflicts=("cover up",)),
    "hat": TypeSpec((("hat", "cap", "beanie", "fedora"),), conflicts=_CLOTHING_NOUNS + _FOOTWEAR_NOUNS,
                    masks=("cap sleeve", "toe cap", "cap toe", "shower cap", "knee cap", "hat box")),
    "belt": TypeSpec((("belt",),), conflicts=_CLOTHING_NOUNS + ("bag",),
                     masks=("belt bag", "belt loop", "belt clip")),
    "sock": TypeSpec((("sock",),), conflicts=_FOOTWEAR_NOUNS, masks=("sock sneaker", "sock shoe")),
}

# Colour families: the family name matches every shade in it; a shade matches only itself.
COLOURS: dict[str, tuple[str, ...]] = {
    "black": ("black",),
    "white": ("white", "off white", "ivory"),
    "red": ("red", "burgundy", "maroon", "wine red", "crimson"),
    "blue": ("blue", "navy", "navy blue", "royal blue", "sky blue", "light blue", "dark blue", "baby blue"),
    "green": ("green", "olive", "army green", "olive green", "mint green", "sage", "dark green"),
    "yellow": ("yellow", "mustard"),
    "orange": ("orange",),
    "pink": ("pink", "hot pink", "light pink", "blush", "fuchsia"),
    "purple": ("purple", "violet", "lavender", "lilac"),
    "brown": ("brown", "tan", "camel", "coffee", "chocolate", "dark brown"),
    "grey": ("grey", "gray", "charcoal", "light grey", "light gray", "dark grey", "dark gray"),
    "beige": ("beige", "nude", "khaki"),
    "gold": ("gold", "golden"),
    "silver": ("silver",),
}
_SHADE_FAMILY = {shade: family for family, shades in COLOURS.items() for shade in shades}
# Spellings of the same shade.
_SAME_SHADE = {"navy blue": "navy", "gray": "grey", "light gray": "light grey", "dark gray": "dark grey",
               "golden": "gold"}
# Title phrases that contain a colour word but name something else (brands, prints, sizes).
_COLOUR_MASKS = ("red wing", "white mountain", "old navy", "black friday", "gold toe", "navy seal",
                 "mint condition", "tan line", "white noise", "blackout")

# Materials. "leather" is genuine leather only: faux / vegan / PU leather is its own material.
MATERIALS: dict[str, tuple[str, ...]] = {
    "faux leather": ("faux leather", "vegan leather", "pu leather", "synthetic leather", "leatherette",
                     "imitation leather", "artificial leather", "leather look"),
    "leather": ("leather", "genuine leather", "full grain leather", "cowhide"),
    "suede": ("suede",),
    "canvas": ("canvas",),
    "denim": ("denim",),
    "cotton": ("cotton",),
    "linen": ("linen",),
    "silk": ("silk",),
    "satin": ("satin",),
    "wool": ("wool", "merino"),
    "mesh": ("mesh",),
    "velvet": ("velvet",),
    "knit": ("knit",),
    # Indian textiles and jewellery work (2026-10-23)
    "georgette": ("georgette",),
    "chiffon": ("chiffon",),
    "organza": ("organza",),
    "crepe": ("crepe",),
    "net": ("net fabric", "net saree", "net lehenga", "soft net"),
    "banarasi": ("banarasi", "benarasi", "banarsi"),
    "kanjivaram": ("kanjivaram", "kanjeevaram", "kanchipuram", "kanchi pattu"),
    "kundan": ("kundan",),
    "meenakari": ("meenakari", "minakari"),
    "pearl": ("pearl",),
    "oxidised silver": ("oxidised silver", "oxidized silver", "oxidised", "oxidized", "german silver"),
    "gold plated": ("gold plated", "gold plating", "gold tone", "gold toned"),
    "sterling silver": ("sterling silver", "925 silver", "925 sterling"),
}

# Occasions (2026-10-23): met only when the listing states them (title, description or
# details.Occasion); an unstated occasion is unknown, never assumed. "wedding" includes
# bridal wear; "bridal" is the bride's own. Only distinctive occasions that listings
# actually state are hard constraints: "everyday use" / "casual" describe how something is
# worn, are rarely stated, and would hide good matches, so they stay ordinary search text.
OCCASIONS: dict[str, tuple[str, ...]] = {
    "bridal": ("bridal", "bride", "dulhan"),
    "wedding": ("wedding", "bridal", "bride", "marriage", "shaadi", "sangeet", "mehendi", "reception"),
    "festive": ("festive", "festival", "diwali", "navratri", "garba", "eid", "puja", "pooja", "karwa chauth"),
    "party": ("party", "party wear", "partywear", "cocktail"),
}
_OCCASION_MASKS = ("3rd party", "third party", "party favor", "party favour", "party supplies", "evening primrose")
_MATERIAL_MASKS = ("faux suede", "suede look", "lace up", "lace-up", "leather like", "leather-like")
# A listing that states faux leather and also says plain "leather" ("Coffee Leather Wallet ... PU
# Leather") is genuine leather only if it says so explicitly.
_GENUINE_LEATHER: dict[str, tuple[str, ...]] = {"leather": ("genuine leather", "real leather", "full grain leather", "full-grain leather",
                                "top grain leather", "top-grain leather", "cowhide")}


def _remove_phrases(tokens: list[str], phrases: tuple[str, ...]) -> list[str]:
    """Tokens with every occurrence of the phrases replaced by a separator (so neighbours don't join)."""
    out = list(tokens)
    for phrase in phrases:
        target = phrase.split()
        n = len(target)
        i = 0
        while i <= len(out) - n:
            if out[i:i + n] == target:
                out[i:i + n] = ["|"]
            else:
                i += 1
    return out


def strict_type_match(title: Optional[str], ptype: str) -> Optional[str]:
    """The phrase in ``title`` that states ``ptype`` under its strict rule, else None."""
    return _strict_type_match(title or "", ptype)


@lru_cache(maxsize=500_000)
def _strict_type_match(title: str, ptype: str) -> Optional[str]:
    # Whole-word phrase tests on a space-padded token string: every product title is
    # checked against every rule on each turn, so this must stay cheap (and is cached).
    spec = STRICT_TYPES[ptype]
    raw = _WORD.findall(title.lower()) if spec.literal else words(title)
    text = " " + " ".join(raw) + " "
    for mask in spec.masks:
        needle = f" {mask} "
        while needle in text:
            text = text.replace(needle, " | ")
    if any(f" {c} " in text for c in spec.conflicts):
        return None
    found = []
    for group in spec.all_of:
        hit = next((ph for ph in group if f" {ph} " in text), None)
        if hit is None:
            return None
        found.append(hit)
    return " + ".join(dict.fromkeys(found))


def _details(product: dict[str, Any]) -> dict[str, Any]:
    raw = product.get("attributes_json")
    attrs = product.get("attributes") if isinstance(product.get("attributes"), dict) else None
    if attrs is None and raw:
        try:
            attrs = json.loads(raw)
        except (TypeError, ValueError):
            attrs = None
    details = (attrs or {}).get("details")
    return details if isinstance(details, dict) else {}


def _found(text: Optional[str], vocab: dict[str, tuple[str, ...]], masks: tuple[str, ...]) -> list[str]:
    """Vocabulary entries stated in ``text`` (longest phrase first, no overlaps)."""
    tokens = _remove_phrases(words(text), masks)
    hits, used = [], set()
    phrases = sorted(((ph, key) for key, phs in vocab.items() for ph in phs), key=lambda x: -len(x[0].split()))
    for phrase, key in phrases:
        for pos in _phrase_positions(tokens, phrase):
            span = set(range(pos, pos + len(phrase.split())))
            if span & used:
                continue
            used |= span
            hits.append(_SAME_SHADE.get(phrase, phrase) if vocab is COLOURS else key)
    return list(dict.fromkeys(hits))


def product_colours(product: dict[str, Any]) -> tuple[list[str], Optional[str]]:
    """(colour shades, source) stated by the listing: details.Color first, else the title. Empty = unknown."""
    structured = _found(str(_details(product).get("Color") or ""), COLOURS, _COLOUR_MASKS)
    if structured:
        return structured, "listing details"
    titled = _found(product.get("title"), COLOURS, _COLOUR_MASKS)
    return (titled, "title") if titled else ([], None)


def product_materials(product: dict[str, Any]) -> tuple[list[str], Optional[str]]:
    """(materials, source) stated by the listing: details.Material first, else the title. Empty = unknown."""
    for source, text in (("listing details", str(_details(product).get("Material") or "")),
                         ("title", product.get("title"))):
        found = _found(text, MATERIALS, _MATERIAL_MASKS)
        if "leather" in found and "faux leather" in found and not _found(text, _GENUINE_LEATHER, _MATERIAL_MASKS):
            found.remove("leather")
        if found:
            return found, source
    return [], None


def _occasions_in(text: Optional[str]) -> list[str]:
    """Every occasion stated in ``text``; each is checked on its own, so "bridal" wear is also
    "wedding" wear (the vocabularies overlap on purpose)."""
    padded = " " + " ".join(_remove_phrases(words(text), _OCCASION_MASKS)) + " "
    return [key for key, phrases in OCCASIONS.items() if any(f" {ph} " in padded for ph in phrases)]


def product_occasions(product: dict[str, Any]) -> tuple[list[str], Optional[str]]:
    """(occasions, source) stated by the listing: details.Occasion, else title, else description."""
    for source, text in (("listing details", str(_details(product).get("Occasion") or "")),
                         ("title", product.get("title")), ("description", product.get("description"))):
        found = _occasions_in(text)
        if found:
            return found, source
    return [], None


def detect_occasions(text: str) -> list[str]:
    """Occasions named in a query ("bridal", "wedding", "festive", "party", "everyday")."""
    # In a query "bride"/"wedding" words name one occasion each: "bridal" wins over "wedding"
    # only when the user says bridal/bride.
    found = _found(text, OCCASIONS, _OCCASION_MASKS)
    if "bridal" in found and "wedding" in found and not any(w in words(text) for w in ("wedding", "marriage")):
        found.remove("wedding")
    return found


def detect_colours(text: str) -> list[str]:
    """Colour words in a query, as stated (a family name like "blue" or a shade like "navy")."""
    return _found(text, COLOURS, _COLOUR_MASKS)


def detect_materials(text: str) -> list[str]:
    """Materials named in a query ("leather", "faux leather", "denim" ...)."""
    return _found(text, MATERIALS, _MATERIAL_MASKS)


def _norm_name(text: Optional[str]) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


def brand_matches(product: dict[str, Any], brand: str) -> Optional[str]:
    """Where the listing states ``brand``: its brand field, its store name, or the start of its title.

    The stated name must equal the brand or start with it as whole words ("Sony Audio" for
    "sony"), ignoring case and punctuation; never a substring inside a word.
    """
    want = _norm_name(brand)
    if not want:
        return None

    def same(name: Optional[str]) -> bool:
        have = _norm_name(name)
        return have == want or have.startswith(want + " ")

    if same(product.get("brand_name") or product.get("brand")):
        return "brand"
    raw = product.get("attributes_json")
    attrs = product.get("attributes") if isinstance(product.get("attributes"), dict) else None
    if attrs is None and raw:
        try:
            attrs = json.loads(raw)
        except (TypeError, ValueError):
            attrs = None
    if isinstance(attrs, dict) and same(attrs.get("store")):
        return "store"
    if same(product.get("title")):
        return "title"
    return None


def colour_matches(requested: str, shades: list[str]) -> bool:
    """A family ("blue") accepts any of its shades; a shade ("navy") accepts only itself."""
    if requested in COLOURS:
        return any(_SHADE_FAMILY.get(s) == requested for s in shades)
    return requested in shades


CURRENCY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("INR", re.compile(r"₹|\brs\.?(?=\s*\d)|\binr\b|\brupees?\b", re.IGNORECASE)),
    ("USD", re.compile(r"\$|\busd\b|\bdollars?\b", re.IGNORECASE)),
    ("EUR", re.compile(r"€|\beur\b|\beuros?\b", re.IGNORECASE)),
    ("GBP", re.compile(r"£|\bgbp\b|\bpounds?\b(?!\s*(?:of|weight))", re.IGNORECASE)),
)

_WORD = re.compile(r"[a-z0-9]+")


def singular(word: str) -> str:
    """Crude English singular form, enough for product nouns (shoes -> shoe, watches -> watch)."""
    w = word.lower()
    if w == "mice":
        return "mouse"
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("ches", "shes", "xes", "sses", "zes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us")):
        return w[:-1]
    return w


@lru_cache(maxsize=300_000)
def _words_cached(text: str) -> tuple[str, ...]:
    return tuple(singular(w) for w in _WORD.findall(text.lower()))


def words(text: Optional[str]) -> list[str]:
    """Lower-case, singularised word tokens (cached: strict filters re-read every title each turn)."""
    return list(_words_cached(text or ""))


def _phrase_positions(tokens: list[str], phrase: str) -> list[int]:
    target = phrase.split()
    n = len(target)
    return [i for i in range(len(tokens) - n + 1) if tokens[i:i + n] == target]


# Query phrases that contain a type word but do not ask for that type ("short sleeve" is not shorts).
QUERY_MASKS = ("short sleeve", "short sleeved", "cap sleeve", "boot cut", "bootcut", "waist trainer",
               "heel height", "flat heel", "no heel")


def detect_product_types(text: str) -> list[str]:
    """Product types named in ``text``, longest phrase first, without overlaps.

    "laptop stand" is detected as the single type "laptop stand", not also "laptop".
    """
    tokens = _remove_phrases(words(text), QUERY_MASKS)
    candidates = []
    for ptype, phrases in PRODUCT_TYPES.items():
        for phrase in phrases:
            for pos in _phrase_positions(tokens, phrase):
                candidates.append((len(phrase.split()), pos, len(phrases), ptype))
    used: set[int] = set()
    found: list[str] = []
    # Longest phrase first; on a tie, the most specific type (fewest synonyms), so
    # "earbuds" means earbuds, while "headphones" means the broader headphones type.
    for length, pos, _, ptype in sorted(candidates, key=lambda c: (-c[0], c[1], c[2])):
        span = set(range(pos, pos + length))
        if span & used:
            continue
        used |= span
        if ptype not in found:
            found.append(ptype)
    return found


def product_type_for_term(term: str) -> Optional[str]:
    """Map a free-text category/term (e.g. from the LLM parser) to a known product type."""
    types = detect_product_types(term)
    return types[0] if types else None


def type_phrases(ptype: str) -> tuple[str, ...]:
    """Synonym phrases for a type. Unknown types are matched literally (singularised)."""
    return PRODUCT_TYPES.get(ptype) or (" ".join(words(ptype)),)


def find_phrase(product: dict[str, Any], phrases: tuple[str, ...]) -> Optional[tuple[str, str]]:
    """First (field, phrase) where a phrase occurs as whole words in title, then description."""
    for field in ("title", "description"):
        tokens = words(product.get(field))
        for phrase in phrases:
            if phrase and _phrase_positions(tokens, phrase):
                return field, phrase
    return None


def match_product_type(product: dict[str, Any], ptypes: tuple[str, ...]) -> Optional[tuple[str, str, str]]:
    """(type, field, phrase) for the first requested type the product matches, else None.

    Strict (Fashion) types are checked against the title only, with their conflict and
    accessory rules; other types match a synonym in the title or description.
    """
    for ptype in ptypes:
        if ptype in STRICT_TYPES:
            phrase = strict_type_match(product.get("title"), ptype)
            if phrase:
                return ptype, "title", phrase
            continue
        hit = find_phrase(product, type_phrases(ptype))
        if hit:
            return ptype, hit[0], hit[1]
    return None


def mentions_term(product: dict[str, Any], term: str) -> Optional[str]:
    """Field ('title'/'description') whose words contain ``term``, else None."""
    hit = find_phrase(product, (" ".join(words(term)),))
    return hit[0] if hit else None


def detect_price_currency(text: str) -> Optional[str]:
    """ISO code of the currency named in the message, or None if none is stated.

    Returns the first currency mentioned. Nothing is converted: Self Search has
    no exchange-rate data.
    """
    best: Optional[tuple[int, str]] = None
    for code, pattern in CURRENCY_PATTERNS:
        m = pattern.search(text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), code)
    return best[1] if best else None
