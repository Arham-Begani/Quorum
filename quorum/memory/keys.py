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

# --- flights ---------------------------------------------------------------
_register("flight.arrival_date", "arrival date", "arrives", "flight arrival",
          "flight.arrives_on")
_register("flight.departure_date", "departure date", "departs", "flight departure",
          "flight.departs_on")
_register("flight.number", "flight no", "flight number", "flight_no")

# --- ground transport ------------------------------------------------------
_register("ground.transfer_slot", "transfer slot", "airport transfer",
          "transfer", "ground transfer", "pickup slot", "ground.transfer")
_register("ground.pickup_time", "pickup time", "pick up time", "collection time")

# --- budget and policy -----------------------------------------------------
_register("budget.ceiling_usd", "budget ceiling", "budget cap", "max budget",
          "spend limit", "budget.cap", "budget_limit", "ceiling")
_register("budget.currency", "currency")

# --- traveller preferences -------------------------------------------------
_register("traveller.contact_preference", "contact preference",
          "communication preference", "contact pref", "traveler.contact_preference",
          "notification preference", "email preference")
_register("traveller.price_flexibility", "price flexibility",
          "traveler.price_flexibility", "flexible on price", "price sensitivity")
_register("traveller.loyalty_program", "loyalty program", "frequent flyer",
          "traveler.loyalty_program")


def normalize_attribute(attribute: str) -> tuple[str, bool]:
    """Return (canonical_attribute, was_mapped)."""
    slug = _slug(attribute)
    if not slug:
        return "unknown", False
    if slug in ALIAS_MAP:
        return ALIAS_MAP[slug], True
    # Unknown but well-formed: pass through normalized so it still groups
    # consistently, and count it so coverage is measurable.
    metrics.count_unmapped_attribute(slug)
    return slug, False


def normalize_entity_id(entity_id: str | int) -> str:
    slug = _slug(entity_id)
    return slug or "unknown"


def normalize(entity_type: str, entity_id: str | int, attribute: str) -> str:
    """Build the canonical subject_key: 'trip:42:hotel.checkin_date'."""
    etype = _slug(entity_type) or "entity"
    eid = normalize_entity_id(entity_id)
    attr, _ = normalize_attribute(attribute)
    return f"{etype}:{eid}:{attr}"


def parse(subject_key: str) -> tuple[str, str, str]:
    """Inverse of normalize, for display. Attribute may itself contain ':'-free dots."""
    parts = subject_key.split(":", 2)
    if len(parts) != 3:
        return ("entity", "unknown", subject_key)
    return (parts[0], parts[1], parts[2])


def coverage() -> dict:
    """How much of what we saw was actually in the alias map."""
    unmapped = dict(metrics.unmapped_attributes)
    return {
        "canonical_attributes": sorted(set(ALIAS_MAP.values())),
        "alias_count": len(ALIAS_MAP),
        "unmapped_attributes": unmapped,
        "unmapped_distinct": len(unmapped),
    }
