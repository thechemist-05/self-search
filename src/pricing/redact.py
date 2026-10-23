"""Keep foreign-currency prices out of anything shown to a user (2026-10-23).

Self Search shows prices only in INR. The model is given INR prices only, but it could
still write a dollar (or euro, pound) amount; ``redact_foreign_prices`` replaces any such
amount in answer text with a neutral marker. It never converts or guesses a value.
"""

from __future__ import annotations

import re

_AMOUNT = r"[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?"
# "$23.69", "US$ 24", "USD 23.69", "23.69 USD", "24 dollars", "€10", "EUR 10", "£5", "5 pounds sterling"
FOREIGN_PRICE = re.compile(
    rf"(?:(?:US\s?\$|\$|€|£)\s?(?:{_AMOUNT})"
    rf"|\b(?:USD|EUR|GBP)\s?(?:{_AMOUNT})\b"
    rf"|\b(?:{_AMOUNT})\s?(?:USD|EUR|GBP|US\s?dollars?|dollars?|euros?|pounds\s+sterling)\b)",
    re.IGNORECASE,
)
REDACTED = "[price in ₹ shown on the card]"


def contains_foreign_price(text: str) -> bool:
    """True if ``text`` states an amount in a currency other than INR."""
    return bool(text) and FOREIGN_PRICE.search(text) is not None


def redact_foreign_prices(text: str) -> str:
    """``text`` with every foreign-currency amount replaced by a neutral marker."""
    return FOREIGN_PRICE.sub(REDACTED, text) if text else text
