"""subject_key normalization, including adversarial spacing/casing/aliasing.

High value: bad normalization silently degrades detection. Two agents writing
about the same attribute with different keys means tier 1 never fires and the
contradiction is missed entirely.
"""

from __future__ import annotations

import pytest

from quorum.memory import keys


@pytest.mark.parametrize("attribute", [
    "checkin_date", "check_in_date", "Check-In Date", "check in date",
    "CHECKIN", "  check-in date  ", "checkin date", "hotel.check_in_date",
])
def test_checkin_aliases_all_normalize_to_one_key(attribute):
    assert keys.normalize("trip", 1, attribute) == "trip:1:hotel.checkin_date"


@pytest.mark.parametrize("attribute", [
    "budget ceiling", "budget_cap", "Max Budget", "spend limit", "BUDGET.CAP",
])
def test_budget_aliases(attribute):
    assert keys.normalize("trip", 42, attribute) == "trip:42:budget.ceiling_usd"


def test_entity_id_normalization():
    assert keys.normalize("Trip", " 42 ", "checkin") == "trip:42:hotel.checkin_date"
    assert keys.normalize("TRIP", "abc-123", "checkin") == "trip:abc_123:hotel.checkin_date"


def test_accents_and_punctuation_are_stripped():
    assert keys._slug("Café/Rate") == "cafe_rate"
    assert keys._slug("a---b") == "a_b"
    assert keys._slug("!!!") == ""


def test_unknown_attribute_passes_through_and_is_counted():
    from quorum.db.metrics import metrics
    metrics.reset()
    k = keys.normalize("trip", 1, "some brand new attribute")
    assert k == "trip:1:some_brand_new_attribute"
    assert "some_brand_new_attribute" in metrics.unmapped_attributes


def test_known_attribute_is_not_counted_as_unmapped():
    from quorum.db.metrics import metrics
    metrics.reset()
    keys.normalize("trip", 1, "Check-In Date")
    assert metrics.unmapped_attributes == {}


def test_arrival_date_is_deliberately_not_aliased_to_checkin():
    """A flight landing and a hotel stay starting are different facts.

    Conflating them would manufacture contradictions that do not exist, which
    is worse than missing one -- it destroys trust in every detection.
    """
    assert keys.normalize("trip", 1, "arrival date") == "trip:1:flight.arrival_date"
    assert keys.normalize("trip", 1, "checkin") == "trip:1:hotel.checkin_date"


def test_parse_roundtrip():
    k = keys.normalize("trip", 7, "checkout")
    assert keys.parse(k) == ("trip", "7", "hotel.checkout_date")


def test_parse_handles_malformed():
    assert keys.parse("garbage")[2] == "garbage"


def test_empty_attribute_is_stable():
    assert keys.normalize("trip", 1, "") == "trip:1:unknown"


def test_coverage_reports_canonical_set():
    cov = keys.coverage()
    assert "hotel.checkin_date" in cov["canonical_attributes"]
    assert cov["alias_count"] > len(cov["canonical_attributes"])
