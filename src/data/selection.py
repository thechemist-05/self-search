"""Versioned product-selection rules (``config/selection_amazon2023.toml``).

A product's type comes from whole-word / whole-phrase matches in its title; the
first type in file order wins and a type's exclude phrases veto it. A category may
also list exclude phrases that veto every type in it (accessories such as socks or
keychains whose titles mention shoes or bags). The rules
never look at prices, so selection is independent of the price source.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class SelectionConfigError(ValueError):
    """The selection file is missing fields or has invalid values."""


def has_phrase(text: str, phrase: str) -> bool:
    """Case-insensitive whole-word/phrase match (letters and digits are word characters)."""
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase.casefold())}(?![a-z0-9])", text.casefold()) is not None


@dataclass(frozen=True)
class TypeRule:
    key: str
    title_keywords: tuple[str, ...]
    exclude_title_keywords: tuple[str, ...] = ()
    # selection-v4+: a priority type is filled before the balanced split (up to
    # ``max_products`` if set); the rest of the quota is shared by the other types.
    priority: bool = False
    max_products: Optional[int] = None
    # selection-v4 allocation: the group (allocation bucket) this type belongs to, and words of
    # which at least one must also be present ("men" for men's shirts, "saree" for silk sarees).
    group: Optional[str] = None
    requires: tuple[str, ...] = ()

    def matches(self, title: str) -> bool:
        return (any(has_phrase(title, k) for k in self.title_keywords)
                and (not self.requires or any(has_phrase(title, k) for k in self.requires))
                and not any(has_phrase(title, k) for k in self.exclude_title_keywords))


@dataclass(frozen=True)
class CategoryRules:
    name: str
    quota: int
    types: tuple[TypeRule, ...]
    exclude_title_keywords: tuple[str, ...] = ()
    # selection-v4: allocation bucket -> number of products to select (sum = quota). Types in a
    # group share its quota with the balanced split; types in no group are never selected.
    groups: tuple[tuple[str, int], ...] = ()

    def excluded_by(self, title: str) -> Optional[str]:
        """The first category-wide exclude phrase in the title, if any."""
        return next((k for k in self.exclude_title_keywords if has_phrase(title or "", k)), None)

    def type_for(self, title: str) -> Optional[str]:
        if self.excluded_by(title):
            return None
        return next((t.key for t in self.types if t.matches(title or "")), None)


def title_key(title: str) -> str:
    """Normalised title used to spot the same listing under several ASINs (case, punctuation, spacing)."""
    return " ".join(re.findall(r"[a-z0-9]+", (title or "").casefold()))


# Words that distinguish variants of one listing rather than different products.
_VARIANT_WORDS = frozenset("""
black white red blue navy green yellow orange pink purple violet grey gray brown beige tan cream ivory
gold golden silver rose maroon burgundy wine teal turquoise aqua mint olive khaki peach coral magenta
mustard lavender lilac multicolor multicolour multi color colour colors colours light dark
small medium large xl xxl xxxl xs s m l size sizes free one pack pcs pc piece pieces set of
""".split())


def near_title_key(title: str, store: Optional[str] = None) -> str:
    """Near-duplicate key (selection-v4): the same seller's listing with only colour, size or
    count words changed ("Red Kundan Jhumka Earrings" / "Green Kundan Jhumka Earrings").

    Numbers and variant words are dropped; the store (seller) is part of the key, so two
    sellers' similar titles are never merged. An empty remainder never matches anything.
    """
    words = [w for w in re.findall(r"[a-z]+", (title or "").casefold()) if w not in _VARIANT_WORDS]
    if len(words) < 3:
        return ""
    return f"{(store or '').casefold().strip()}|{' '.join(words)}"


@dataclass(frozen=True)
class SelectionConfig:
    version: str
    sha256: str
    min_qualifying_reviews: int
    min_review_chars: int
    max_reviews_per_product: int
    categories: dict[str, CategoryRules]
    # selection-v3+: of products whose normalised titles are identical, keep only the one with the
    # most qualifying reviews (the same listing repeated under several ASINs). Off for v1/v2.
    dedupe_identical_titles: bool = False


def load_selection_config(path: Path) -> SelectionConfig:
    raw = Path(path).read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise SelectionConfigError(f"unreadable selection file: {exc}") from exc
    ints = {}
    for key in ("min_qualifying_reviews", "min_review_chars", "max_reviews_per_product"):
        value = data.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise SelectionConfigError(f"'{key}' must be a positive integer")
        ints[key] = value
    if not str(data.get("version") or "").strip():
        raise SelectionConfigError("missing 'version'")
    categories = {}
    for name in ("fashion", "electronics", "clothing", "beauty", "handmade", "sports"):
        if name not in data:
            continue  # a version may select from one category only
        section = data[name]
        quota = section.get("quota")
        if not isinstance(quota, int) or quota < 1:
            raise SelectionConfigError(f"[{name}] needs a positive quota")
        types, seen = [], set()
        for t in section.get("type") or []:
            key = str(t.get("key") or "")
            if not key or key in seen or not t.get("title_keywords"):
                raise SelectionConfigError(f"[{name}] type {key!r}: missing/duplicate key or no keywords")
            seen.add(key)
            cap = t.get("max_products")
            if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 1):
                raise SelectionConfigError(f"[{name}] type {key!r}: max_products must be a positive integer")
            types.append(TypeRule(key, tuple(t["title_keywords"]), tuple(t.get("exclude_title_keywords") or ()),
                                  priority=bool(t.get("priority", False)), max_products=cap,
                                  group=t.get("group"), requires=tuple(t.get("requires") or ())))
        if not types:
            raise SelectionConfigError(f"[{name}] has no types")
        groups = section.get("groups") or {}
        if groups:
            if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in groups.values()):
                raise SelectionConfigError(f"[{name}.groups] quotas must be non-negative integers")
            if sum(groups.values()) != quota:
                raise SelectionConfigError(f"[{name}] group quotas sum to {sum(groups.values())}, not {quota}")
            unknown = {t.group for t in types if t.group and t.group not in groups}
            if unknown:
                raise SelectionConfigError(f"[{name}] types name unknown groups {sorted(unknown)}")
        categories[name] = CategoryRules(name, quota, tuple(types),
                                         tuple(section.get("exclude_title_keywords") or ()),
                                         tuple(groups.items()))
    if not categories:
        raise SelectionConfigError("no category section ([fashion], [clothing], [beauty], ...)")
    dedupe = data.get("dedupe_identical_titles", False)
    if not isinstance(dedupe, bool):
        raise SelectionConfigError("'dedupe_identical_titles' must be true or false")
    return SelectionConfig(version=data["version"], sha256=hashlib.sha256(raw).hexdigest(),
                           categories=categories, dedupe_identical_titles=dedupe, **ints)


def allocate(quota: int, capacities: dict[str, int]) -> dict[str, int]:
    """Balanced quota split: equal shares, with unused share passed on to types that have more.

    Deterministic water-filling: never allocates more than a type has.
    """
    alloc = {k: 0 for k in capacities}
    remaining, open_types = quota, sorted(k for k, c in capacities.items() if c > 0)
    while remaining > 0 and open_types:
        share = max(remaining // len(open_types), 1)
        progressed = False
        for k in list(open_types):
            if remaining == 0:
                break
            take = min(share, capacities[k] - alloc[k], remaining)
            alloc[k] += take
            remaining -= take
            progressed = progressed or take > 0
            if alloc[k] >= capacities[k]:
                open_types.remove(k)
        if not progressed:
            break
    return alloc


def allocate_with_priority(quota: int, capacities: dict[str, int], types: tuple[TypeRule, ...]) -> dict[str, int]:
    """Priority types first (each up to its ``max_products``), then a balanced split of the rest.

    Without priority types this is exactly ``allocate``.
    """
    alloc = {k: 0 for k in capacities}
    remaining = quota
    for t in types:
        if t.priority and t.key in capacities:
            take = min(capacities[t.key], t.max_products or capacities[t.key], remaining)
            alloc[t.key], remaining = take, remaining - take
    rest = {k: (c if k not in {t.key for t in types if t.priority} else 0) for k, c in capacities.items()}
    for k, v in allocate(remaining, rest).items():
        alloc[k] += v
    return alloc


def allocate_groups(rules: CategoryRules, capacities: dict[str, int]) -> dict[str, int]:
    """Per-type allocation for grouped rules: each group's quota is split across its types with
    the balanced split (``allocate``), so every sub-style is represented. Never more than a type
    has; a group whose types have too few products is filled as far as possible (the shortfall
    is reported, never padded from other groups)."""
    alloc = {k: 0 for k in capacities}
    for group, quota in rules.groups:
        members = {t.key: capacities.get(t.key, 0) for t in rules.types if t.group == group}
        for k, v in allocate(quota, members).items():
            alloc[k] = alloc.get(k, 0) + v
    return alloc
