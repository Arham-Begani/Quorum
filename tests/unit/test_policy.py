"""R1-R4 in isolation, plus rule-ordering, plus the short-circuits.

Each canonical scenario must land on its expected rule; those assertions are at
the bottom and they are what keep §8 honest.
"""

from __future__ import annotations

import uuid

import pytest

from quorum.memory.schema import Atom, Claim, Resolution, Verdict
from quorum.policy import engine, rules

WS = uuid.uuid4()
KEY = "trip:1:hotel.checkin_date"


def claim(role="lodging_agent", conf=0.6, value="2026-09-14"):
    return Claim(WS, KEY, "equals", f"check-in {value}", {"date": value},
                 "agent-1", role, conf)


def atom(role="lodging_agent", conf=0.6, evidence=1, value="2026-09-15"):
    return Atom(id=uuid.uuid4(), workspace_id=WS, subject_key=KEY, predicate="equals",
                object_text=f"check-in {value}", object_json={"date": value},
                writer_agent_id="agent-0", writer_role=role, confidence=conf,
                evidence_count=evidence)


def resolve(c, a, verdict=Verdict.CONTRADICTION):
    return engine.resolve_pair(c, a, verdict)


# --- short circuits --------------------------------------------------------

def test_agreement_short_circuits_to_reinforce():
    d = resolve(claim(), atom(), Verdict.AGREEMENT)
    assert (d.resolution, d.policy_rule) == (Resolution.REINFORCE, "agreement")


def test_refinement_short_circuits_to_supersede():
    d = resolve(claim(), atom(), Verdict.REFINEMENT)
    assert (d.resolution, d.policy_rule) == (Resolution.SUPERSEDE, "refinement")


def test_unrelated_short_circuits_to_accept():
    d = resolve(claim(), atom(), Verdict.UNRELATED)
    assert (d.resolution, d.policy_rule) == (Resolution.ACCEPT, "unrelated")
