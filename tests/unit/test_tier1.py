"""Every structural classification branch, plus the value coercion under it.

Date/number coercion is a silent-miss source: if "2026-09-14" and "Sep 14 2026"
compare unequal, tier 1 reports a contradiction that is not there.
"""

from __future__ import annotations

import uuid

import pytest

from quorum.detect import coerce, tier1
from quorum.memory.schema import Atom, Claim, Verdict

WS = uuid.uuid4()
KEY = "trip:1:hotel.checkin_date"


def claim(value, predicate="equals", key=KEY, role="lodging_agent", conf=0.6,
          text=None):
    return Claim(WS, key, predicate, text or f"value is {value}",
                 value if isinstance(value, dict) or value is None else {"value": value},
                 "agent-1", role, conf)


def atom(value, predicate="equals", key=KEY, role="lodging_agent", conf=0.6,
         evidence=1, text=None):
    return Atom(
        id=uuid.uuid4(), workspace_id=WS, subject_key=key, predicate=predicate,
        object_text=text or f"value is {value}",
        object_json=value if isinstance(value, dict) or value is None else {"value": value},
        writer_agent_id="agent-0", writer_role=role, confidence=conf,
        evidence_count=evidence)


# --- coercion --------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("2026-09-14", "Sep 14 2026"),
    ("2026-09-14", "14 September 2026"),
    ("2026-09-14", "September 14, 2026"),
    ("2026-9-4", "Sep 4 2026"),
])
def test_equivalent_date_spellings_compare_equal(a, b):
    assert coerce.values_equal(a, b)


def test_different_dates_compare_unequal():
    assert not coerce.values_equal("2026-09-14", "2026-09-15")


@pytest.mark.parametrize("a,b", [
    ("$2,400", 2400), ("2400.00", 2400), (" 2400 USD ", 2400),
])
def test_money_spellings_compare_equal(a, b):
    assert coerce.values_equal(a, b)


def test_bool_is_not_treated_as_number():
    assert coerce.coerce_number(True) is None
    assert coerce.values_equal("yes", True)


def test_scalar_extraction():
    assert coerce.scalar_of({"date": "2026-09-14"}) == "2026-09-14"
    assert coerce.scalar_of({"value": 2400}) == 2400
    assert coerce.scalar_of("plain") == "plain"
    assert coerce.scalar_of({"min": 1, "max": 5}) is None
    assert coerce.scalar_of(None) is None


def test_range_extraction_and_membership():
    rng = coerce.range_of({"min": 1000, "max": 3000})
    assert rng == (1000.0, 3000.0)
    assert coerce.point_in_range(2400, rng)
    assert not coerce.point_in_range(3200, rng)
