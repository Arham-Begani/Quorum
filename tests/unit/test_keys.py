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
