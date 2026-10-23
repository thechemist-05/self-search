"""
Query parser for Self Search (Phase 7).

Extracts structured ``ParsedQuery`` from raw user messages using:
1. Rule-based parser — deterministic regex/keyword extraction. In the default
   ``rules_first`` mode it handles every message it can safely handle, so most
   turns need no model call for query understanding.
2. LLM-based parsing — sends the message through ``LLMProvider`` with a
   dedicated parsing prompt and validates against Pydantic schema. Used for
   messages the rules cannot capture (a named brand), or for every message in
   ``llm_first`` mode; the rule-based parser remains the fallback.

The parser NEVER accesses the product catalog. It only parses user language.
Retrieval happens downstream in the intent router.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from pydantic import ValidationError
import structlog

from src.agent.prompts import build_query_parse_messages
from src.agent.schemas import ParsedQuery, UserIntent
from src.config.settings import get_settings
from src.llm.deadline import remaining_seconds
from src.retrieval.constraints import (
    detect_colours,
    detect_occasions,
    detect_materials,
    detect_price_currency,
    detect_product_types,
    product_type_for_term,
)
from src.llm.provider import LLMProvider
from src.llm.service import extract_json_from_text

logger = structlog.get_logger("shelfsense.agent.query_parser")

# ---------------------------------------------------------------------------
# Rule-based patterns
# ---------------------------------------------------------------------------

# Price patterns: "under $100", "below 3000", "above $50", "between $50 and $200",
# "under Rs 4000", "under 4000 rupees". The currency is detected separately
# (src/retrieval/constraints.py); these capture the amount and the whole phrase.
_CUR_BEFORE = r"(?:[\$₹€£]|(?:rs\.?|inr|usd|eur|gbp)\s)?\s*"
_CUR_AFTER = r"(?:\s*(?:rupees?|rs\.?|inr|dollars?|usd|euros?|eur|pounds?|gbp)\b)?"
_PRICE_UNDER = re.compile(
    r"(?:under|below|less\s+than|up\s+to|max(?:imum)?|budget(?:\s+of|\s+is)?)\s*"
    + _CUR_BEFORE + r"(\d+(?:[.,]\d+)?)" + _CUR_AFTER,
    re.IGNORECASE,
)
_PRICE_ABOVE = re.compile(
    r"(?:above|over|more\s+than|min(?:imum)?|at\s+least|starting(?:\s+at)?)\s*"
    + _CUR_BEFORE + r"(\d+(?:[.,]\d+)?)" + _CUR_AFTER,
    re.IGNORECASE,
)
_PRICE_BETWEEN = re.compile(
    r"between\s*" + _CUR_BEFORE + r"(\d+(?:[.,]\d+)?)" + _CUR_AFTER
    + r"\s*(?:and|to|-)\s*" + _CUR_BEFORE + r"(\d+(?:[.,]\d+)?)" + _CUR_AFTER,
    re.IGNORECASE,
)

# Rating patterns: "4+ stars", "rated above 4", "highly rated"
_RATING_EXPLICIT = re.compile(
    r"(\d(?:\.\d)?)\+?\s*(?:stars?|rating|rated)",
    re.IGNORECASE,
)
_RATING_ABOVE = re.compile(
    r"(?:rated?\s+(?:above|over|at\s+least))\s*(\d(?:\.\d)?)",
    re.IGNORECASE,
)
_RATING_QUALITATIVE = re.compile(
    r"\b(highly\s+rated|top[- ]rated|best\s+reviewed|well[- ]reviewed)\b",
    re.IGNORECASE,
)

# Smalltalk patterns
_SMALLTALK_PATTERNS = re.compile(
    r"^\s*(?:hi|hello(?:\s+there)?|hey(?:\s+there)?|howdy|good\s+(?:morning|afternoon|evening)|"
    r"thanks?(?:\s+(?:a\s+lot|so\s+much|very\s+much))?|thank\s+you(?:\s+(?:so\s+much|very\s+much))?|"
    r"bye(?:\s+bye)?|goodbye|see\s+ya|cheers|ok|okay)\s*[!.?]*\s*$",
    re.IGNORECASE,
)

# Comparison patterns
_COMPARE_PATTERNS = re.compile(
    r"\b(?:compare|comparison|versus|vs\.?|side\s+by\s+side|which\s+(?:is|one)\s+better)\b",
    re.IGNORECASE,
)

# Product question patterns
_QUESTION_PATTERNS = re.compile(
    r"\b(?:does\s+it|is\s+it|is\s+this|can\s+it|how\s+(?:is|does|long|well)|"
    r"(?:is|does|can|will)\s+the\s+(?:first|second|third|fourth|fifth|last|1st|2nd|3rd|4th|5th)\b|"
    r"tell\s+me\s+(?:about|more)|what\s+about)\b",
    re.IGNORECASE,
)

# Out-of-scope patterns: clearly non-shopping requests and prompt-injection attempts.
# Deliberately narrow — anything not matched falls through to product search.
_OUT_OF_SCOPE_PATTERNS = re.compile(
    r"\bweather\s+(?:in|at|for|today|tomorrow|like)\b|\bforecast\b|"
    r"\bjokes?\b|\briddle\b|\bhoroscope\b|\blatest\s+news\b|"
    r"\bcapital\s+of\b|\bpresident\s+of\b|\bprime\s+minister\b|"
    r"\bwrite\s+(?:me\s+)?(?:a|an|some)\s+(?:\w+\s+){0,2}"
    r"(?:script|program|code|function|poem|story|essay|song)\b|"
    r"\bignore\s+(?:all\s+)?(?:the\s+|your\s+)?(?:previous|prior|above)\s+instructions\b|"
    r"\bsystem\s+prompt\b",
    re.IGNORECASE,
)

# Words that carry no product signal; a message made only of these is too vague to search.
_VAGUE_WORDS = frozenset({
    "i", "im", "i'm", "me", "my", "we", "a", "an", "the", "some", "something",
    "anything", "stuff", "thing", "things", "item", "items", "product", "products",
    "want", "wanna", "need", "looking", "look", "for", "to", "buy", "get", "find",
    "show", "help", "recommend", "suggest", "please", "nice", "good", "great",
    "cool", "best", "awesome", "can", "could", "would", "you", "like", "any",
    "with", "and", "or", "of", "one", "ones", "really", "very", "new",
})

# Exclusions: "without RGB lights", "not Razer", "no wired options".
_EXCLUSION_PATTERN = re.compile(
    r"\b(?:without|excluding|except|not|no)\s+"
    r"(?!(?:more|less|than|too|over|under|above|below)\b)"
    r"([a-z0-9][\w\-]*(?:\s+(?!(?:and|or|but|with|under|below|above|over|between|for)\b)"
    r"[a-z0-9][\w\-]*){0,2})",
    re.IGNORECASE,
)
_EXCLUSION_TRAILING_NOUNS = re.compile(
    r"\s+(?:options?|ones?|models?|products?|items?|stuff)$", re.IGNORECASE
)

# Positional references to previously shown results: "the second one", "#2", "product 3".
_ORDINAL_WORDS = {
    "first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3, "fifth": 4, "5th": 4,
}
_ORDINAL_PATTERN = re.compile(
    r"\b(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th)\b|"
    r"\b(last)\s+(?:one|product|item|option)\b|"
    r"(?:#\s*|\b(?:number|option|product|item)\s+)([1-9])\b",
    re.IGNORECASE,
)
_COMPARE_COUNT_PATTERN = re.compile(
    r"\b(?:these|those|the|top|first)\s+(two|three|four|2|3|4)\b|\b(both)\b",
    re.IGNORECASE,
)
_COUNT_WORDS = {"two": 2, "three": 3, "four": 4, "2": 2, "3": 3, "4": 4, "both": 2}

# Brand cues the rule-based parser cannot resolve ("from Sony", "by Nike", "any brand"):
# it never extracts a brand, so these messages go to the LLM in rules_first mode.
# "from 500 to 1000" and "by $20" are price phrases, not brands.
_BRAND_CUE_PATTERN = re.compile(
    r"\b(?:from|by)\s+(?![\$₹€£\d]|(?:rs\.?|inr|usd|eur|gbp)\b)[a-z]|\bbrand(?:s|ed)?\b",
    re.IGNORECASE,
)

# Refinement patterns
_REFINE_PATTERNS = re.compile(
    r"^\s*(?:show\s+(?:me\s+)?)?(?:only|just|filter|"
    r"(?:something|anything)\s+(?:cheaper|more\s+expensive|smaller|bigger|lighter|better)|"
    r"(?:no|not|without|exclude)\s+\w+|"
    r"cheaper|more\s+expensive|different|another)\b",
    re.IGNORECASE,
)


def _parse_price(text: str) -> tuple[Optional[float], Optional[float]]:
    """Extract min/max price from text using regex patterns."""
    min_price: Optional[float] = None
    max_price: Optional[float] = None

    m = _PRICE_BETWEEN.search(text)
    if m:
        min_price = float(m.group(1).replace(",", ""))
        max_price = float(m.group(2).replace(",", ""))
        return min_price, max_price

    m = _PRICE_UNDER.search(text)
    if m:
        max_price = float(m.group(1).replace(",", ""))

    m = _PRICE_ABOVE.search(text)
    if m:
        min_price = float(m.group(1).replace(",", ""))

    return min_price, max_price


def _parse_rating(text: str) -> Optional[float]:
    """Extract minimum rating from text using regex patterns."""
    m = _RATING_EXPLICIT.search(text)
    if m:
        return float(m.group(1))

    m = _RATING_ABOVE.search(text)
    if m:
        return float(m.group(1))

    if _RATING_QUALITATIVE.search(text):
        return 4.0

    return None


def _strip_constraint_phrases(text: str) -> str:
    """Remove price and rating phrases from retrieval text.

    "shoe under 4000 rupees, 4+ stars" -> "shoe". The constraints themselves
    are kept as structured fields; leaving their words in the text only adds
    noise ("rupees", "4000") to keyword and semantic search.
    """
    for pattern in (_PRICE_BETWEEN, _PRICE_UNDER, _PRICE_ABOVE, _RATING_ABOVE, _RATING_EXPLICIT, _RATING_QUALITATIVE):
        text = pattern.sub(" ", text)
    text = re.sub(r"\s+([,.;!?])", r"\1", re.sub(r"\s{2,}", " ", text))
    text = re.sub(r"([,;])(\s*[,;.!?])+", r"\1", text)
    return text.strip(" ,;")


def apply_deterministic_constraints(parsed: ParsedQuery) -> ParsedQuery:
    """Attach constraints read deterministically from the user's own words.

    - ``price_currency``: currency stated in the message (₹/Rs/rupees/INR, $/USD, …),
      only when a price bound exists. Never converted.
    - ``product_types``: types named in the message, plus the LLM parser's
      ``category`` when it maps to a known type.
    - ``colours`` / ``materials``: named in the message outside exclusion clauses
      ("black heels" requires black; "heels, not black" excludes it instead).
    Applied after both the LLM and the rule-based parser, so neither path can
    drop or invent these.
    """
    updates: dict[str, Any] = {}
    if parsed.min_price is not None or parsed.max_price is not None:
        updates["price_currency"] = detect_price_currency(parsed.raw_query)
    types = list(parsed.product_types)
    for ptype in detect_product_types(parsed.raw_query):
        if ptype not in types:
            types.append(ptype)
    if parsed.category:
        mapped = product_type_for_term(parsed.category)
        if mapped and mapped not in types:
            types.append(mapped)
    updates["product_types"] = types
    wanted_text = _extract_exclusions(parsed.raw_query)[1]
    updates["colours"] = list(dict.fromkeys([*parsed.colours, *detect_colours(wanted_text)]))
    updates["materials"] = list(dict.fromkeys([*parsed.materials, *detect_materials(wanted_text)]))
    updates["occasions"] = list(dict.fromkeys([*parsed.occasions, *detect_occasions(wanted_text)]))
    return parsed.model_copy(update=updates)


def _is_vague(text: str) -> bool:
    """Return True if the message contains no product-bearing words."""
    tokens = re.findall(r"[a-z0-9']+", text.lower())
    return not [t for t in tokens if t not in _VAGUE_WORDS]


def _extract_exclusions(text: str) -> tuple[list[str], str]:
    """Extract exclusion phrases and return them with the text stripped of them.

    Stripping matters for keyword retrieval: leaving "RGB" in "without RGB"
    would boost exactly the products the user wants to avoid.

    Returns:
        Tuple of (exclusions, text_without_exclusion_clauses).
    """
    exclusions: list[str] = []
    for m in _EXCLUSION_PATTERN.finditer(text):
        phrase = _EXCLUSION_TRAILING_NOUNS.sub("", m.group(1).strip())
        if phrase and phrase not in exclusions:
            exclusions.append(phrase)

    remaining = _EXCLUSION_PATTERN.sub(" ", text)
    remaining = re.sub(r"\s{2,}", " ", remaining)
    remaining = re.sub(r"\s+,", ",", remaining)
    remaining = re.sub(r",(\s*,)+", ",", remaining)
    return exclusions, remaining.strip(" ,;")


def extract_ordinal_references(text: str) -> list[int]:
    """Extract positional references to previously shown results.

    "the second one" -> [1], "compare #1 and #3" -> [0, 2], "the last one" -> [-1].

    Args:
        text: Raw user message.

    Returns:
        Zero-based positions (``-1`` for "last") in order of appearance, deduplicated.
    """
    positions: list[int] = []
    for m in _ORDINAL_PATTERN.finditer(text):
        if m.group(1):
            pos = _ORDINAL_WORDS[m.group(1).lower()]
        elif m.group(2):
            pos = -1
        else:
            pos = int(m.group(3)) - 1
        if pos not in positions:
            positions.append(pos)
    return positions


def extract_compare_count(text: str) -> Optional[int]:
    """Extract how many results the user wants compared ("these two", "top 3", "both").

    Args:
        text: Raw user message.

    Returns:
        Requested count (2–4), or None if not stated.
    """
    m = _COMPARE_COUNT_PATTERN.search(text)
    if not m:
        return None
    return _COUNT_WORDS[(m.group(1) or m.group(2)).lower()]


def _sanitize_llm_constraints(parsed: ParsedQuery, user_message: str) -> ParsedQuery:
    """Drop or repair constraint values from LLM output that cannot be valid.

    - Ratings outside (0, 5] are dropped.
    - Negative prices are dropped.
    - Prices are dropped when the user message contains no digits at all, since
      any such price was invented by the model rather than stated by the user.
    - An inverted price range (min > max) is swapped.
    """
    updates: dict[str, Any] = {}
    min_price, max_price = parsed.min_price, parsed.max_price

    if parsed.min_rating is not None and not 0.0 < parsed.min_rating <= 5.0:
        updates["min_rating"] = None

    if min_price is not None and min_price < 0:
        min_price = None
    if max_price is not None and max_price < 0:
        max_price = None

    if (min_price is not None or max_price is not None) and not re.search(r"\d", user_message):
        logger.info(
            "llm_unstated_price_dropped",
            min_price=parsed.min_price,
            max_price=parsed.max_price,
        )
        min_price = max_price = None

    if min_price is not None and max_price is not None and min_price > max_price:
        min_price, max_price = max_price, min_price

    if (min_price, max_price) != (parsed.min_price, parsed.max_price):
        updates["min_price"] = min_price
        updates["max_price"] = max_price

    if updates:
        logger.info("llm_constraints_sanitized", fields=sorted(updates))
        return parsed.model_copy(update=updates)
    return parsed


def _classify_intent_rule_based(
    text: str,
    has_history: bool = False,
) -> UserIntent:
    """Classify intent using keyword/regex rules as a fallback."""
    stripped = text.strip()

    if not stripped:
        return UserIntent.CLARIFY

    if _SMALLTALK_PATTERNS.match(stripped):
        return UserIntent.SMALLTALK

    if _OUT_OF_SCOPE_PATTERNS.search(stripped):
        return UserIntent.OUT_OF_SCOPE

    if _COMPARE_PATTERNS.search(stripped):
        return UserIntent.COMPARE

    if _QUESTION_PATTERNS.search(stripped) and has_history:
        return UserIntent.PRODUCT_QUESTION

    if has_history and _REFINE_PATTERNS.search(stripped):
        return UserIntent.REFINE_SEARCH

    if _is_vague(stripped) and _parse_price(stripped) == (None, None) and _parse_rating(stripped) is None:
        return UserIntent.CLARIFY

    # Default: treat as product search
    return UserIntent.PRODUCT_SEARCH


def parse_query_rule_based(
    user_message: str,
    has_history: bool = False,
) -> ParsedQuery:
    """Deterministic, offline rule-based query parser.

    Used as a fallback when the LLM is unavailable or returns invalid output.

    Args:
        user_message: Raw user message.
        has_history: Whether there is prior conversation history (affects
            refinement and question detection).

    Returns:
        ParsedQuery with deterministically extracted fields.
    """
    text = user_message.strip()
    intent = _classify_intent_rule_based(text, has_history)

    min_price, max_price = _parse_price(text)
    min_rating = _parse_rating(text)
    exclusions, search_text = _extract_exclusions(text)
    search_text = _strip_constraint_phrases(search_text)

    clarification_question: Optional[str] = None
    if intent == UserIntent.CLARIFY or not text:
        clarification_question = (
            "I'd like to help you find the right product. "
            "Could you describe what you're looking for, including any "
            "preferences for category, brand, budget, or features?"
        )

    return apply_deterministic_constraints(ParsedQuery(
        intent=intent,
        raw_query=user_message,
        search_text=search_text,
        category=None,  # Stored categories are too coarse; product types are detected instead
        brand=None,     # Rule-based parser cannot reliably extract brand
        min_price=min_price,
        max_price=max_price,
        min_rating=min_rating,
        preferences=[],
        exclusions=exclusions,
        compare_product_ids=[],
        follow_up_context=text if intent == UserIntent.REFINE_SEARCH else None,
        clarification_question=clarification_question,
        confidence=0.4,  # Rule-based confidence is lower than LLM
    ))


class QueryParser:
    """Parses raw user messages into structured ``ParsedQuery`` objects.

    In ``rules_first`` mode (the default) the rule-based parser handles every
    message without a brand cue; only those go to the LLM. In ``llm_first``
    mode the LLM parses every message. Either way the LLM is retried once on
    malformed output and the rule-based parser is the fallback.
    """

    CONFIDENCE_THRESHOLD = 0.5
    # Seconds an LLM parse may take; it shares the request's model-time budget with the
    # answer, so a slow parse falls back to the rules instead of starving the answer.
    LLM_PARSE_TIMEOUT_S = 45.0

    def __init__(
        self,
        llm_provider: LLMProvider,
        max_retries: int = 1,
        mode: Optional[str] = None,
    ) -> None:
        self.llm_provider = llm_provider
        self.max_retries = max_retries
        self.mode = mode or get_settings().query_parser_mode
        if self.mode not in ("rules_first", "llm_first"):
            raise ValueError(f"unknown query parser mode: {self.mode!r}")

    def needs_llm(self, text: str) -> bool:
        """Whether this message should be parsed by the LLM (when one is available).

        In ``rules_first`` mode never: the rules read intent, price, rating,
        exclusions, product types, colours and materials, and brands are resolved
        deterministically against the catalogue by ``ConstraintResolver`` (which
        replaced the model call previously made for brand cues such as "from Sony").
        """
        return self.mode == "llm_first"

    def parse(
        self,
        user_message: str,
        conversation_history: Optional[list[dict[str, str]]] = None,
    ) -> ParsedQuery:
        """Parse a user message into a structured query.

        Args:
            user_message: Raw user input.
            conversation_history: Optional prior turns for follow-up detection.

        Returns:
            ParsedQuery with intent, constraints, and confidence score.
        """
        text = user_message.strip()
        has_history = bool(conversation_history)

        # Quick-exit for trivially classifiable messages
        if not text:
            return ParsedQuery(
                intent=UserIntent.CLARIFY,
                raw_query=user_message,
                search_text="",
                clarification_question=(
                    "It looks like your message was empty. "
                    "Could you describe what product you're looking for?"
                ),
                confidence=1.0,
            )

        if _SMALLTALK_PATTERNS.match(text):
            return ParsedQuery(
                intent=UserIntent.SMALLTALK,
                raw_query=user_message,
                search_text="",
                confidence=1.0,
            )

        if not self.needs_llm(text):
            logger.info("rule_based_parse", query=text)
            return parse_query_rule_based(text, has_history)

        # Attempt LLM-based parsing
        if self.llm_provider.is_available():
            parsed = self._parse_with_llm(text, conversation_history)
            if parsed is not None:
                parsed = apply_deterministic_constraints(_sanitize_llm_constraints(parsed, text))
                # Enforce confidence-based clarification
                if parsed.confidence < self.CONFIDENCE_THRESHOLD and parsed.intent != UserIntent.CLARIFY:
                    logger.info(
                        "low_confidence_clarify",
                        confidence=parsed.confidence,
                        original_intent=parsed.intent.value,
                    )
                    parsed = parsed.model_copy(update={
                        "intent": UserIntent.CLARIFY,
                        "clarification_question": (
                            parsed.clarification_question
                            or "I'm not entirely sure what you're looking for. "
                            "Could you provide more details about the type of product, "
                            "your budget, or any specific features you need?"
                        ),
                    })
                return parsed
            logger.warning("llm_parse_failed_using_fallback", query=text)
        else:
            logger.info("llm_unavailable_using_fallback", query=text)

        # Fallback to rule-based parser
        return parse_query_rule_based(text, has_history)

    def _parse_with_llm(
        self,
        user_message: str,
        conversation_history: Optional[list[dict[str, str]]] = None,
    ) -> Optional[ParsedQuery]:
        """Attempt LLM-based parsing with retry on malformed output.

        Returns None if all attempts fail (triggering rule-based fallback).
        """
        messages = build_query_parse_messages(user_message, conversation_history)
        current_messages = list(messages)

        for attempt in range(self.max_retries + 1):
            remaining = remaining_seconds(self.LLM_PARSE_TIMEOUT_S)
            if remaining < 5.0:
                logger.info("llm_parse_skipped_deadline", remaining_s=round(remaining, 1))
                return None
            try:
                response = self.llm_provider.generate(current_messages, timeout=remaining)
                raw_json = extract_json_from_text(response.content)
                parsed_dict = json.loads(raw_json)

                # Ensure raw_query is always the original message
                parsed_dict["raw_query"] = user_message

                # Validate against schema
                parsed = ParsedQuery.model_validate(parsed_dict)

                logger.info(
                    "llm_parse_success",
                    attempt=attempt,
                    intent=parsed.intent.value,
                    confidence=parsed.confidence,
                    latency_ms=response.latency_ms,
                )
                return parsed

            except (json.JSONDecodeError, ValidationError) as e:
                logger.warning(
                    "llm_parse_validation_failed",
                    attempt=attempt,
                    error=str(e),
                )
                if attempt < self.max_retries:
                    current_messages.append({"role": "assistant", "content": response.content})
                    current_messages.append({
                        "role": "user",
                        "content": (
                            f"Your response was invalid: {e}. "
                            "Please return ONLY valid JSON matching the ParsedQuery schema."
                        ),
                    })
            except Exception as e:
                logger.warning("llm_parse_unexpected_error", error=str(e))
                break

        return None
