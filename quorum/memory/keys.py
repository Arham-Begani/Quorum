"""subject_key normalization — the highest-leverage file in the repo.

`subject_key` is the cheap structural handle for conflict detection. If two
agents describe the same attribute with different keys, tier-1 misses the
contradiction entirely and the system falls through to slow, fuzzy, expensive
tier-2 — or misses it altogether. Every normalization bug is a silent
detection failure, which is the worst kind this project can have.

    normalize("trip", "42", "Check-In Date") -> "trip:42:hotel.checkin_date"

Unmapped attributes still normalize (so nothing crashes), but they are counted.
A growing unmapped list means detection coverage is degrading, which is why the
count is surfaced in the run report rather than swallowed.
"""

from __future__ import annotations

import re
import unicodedata

from ..db.metrics import metrics

# Canonical attribute paths for the Atlas Travel domain. Everything on the
# right of a mapping is a genuine synonym for the canonical form — never a
# semantic reinterpretation. "arrival_date" is deliberately NOT mapped onto
# hotel.checkin_date: a flight arriving and a hotel stay beginning are
# different facts that often coincide, and conflating them would manufacture
# contradictions that do not exist.
ALIAS_MAP: dict[str, str] = {}


def _register(canonical: str, *aliases: str) -> None:
    ALIAS_MAP[canonical] = canonical
    for a in aliases:
        ALIAS_MAP[_slug(a)] = canonical


def _slug(text: str) -> str:
    """Lowercase, strip accents, collapse separators to '_'."""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.strip().lower()
    text = re.sub(r"[\s\-/]+", "_", text)
    text = re.sub(r"[^a-z0-9_.]", "", text)
    text = re.sub(r"_{2,}", "_", text)
    return text.strip("._")


# --- lodging ---------------------------------------------------------------
_register("hotel.checkin_date",
          "check in date", "check-in date", "checkin", "check_in", "checkin date",
          "hotel check in", "hotel.check_in_date", "hotel checkin date",
          "lodging.checkin", "hotel.check_in")
_register("hotel.checkout_date",
          "check out date", "check-out date", "checkout", "check_out",
          "hotel.check_out_date", "lodging.checkout", "departure from hotel")
_register("hotel.name", "hotel", "property", "hotel_name", "lodging.name")
_register("hotel.nightly_rate", "nightly rate", "room rate", "rate per night",
          "hotel.rate", "hotel_price")
