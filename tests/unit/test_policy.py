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


# --- R1 authority ----------------------------------------------------------

def test_r1_higher_authority_supersedes():
    d = resolve(claim(role="booking_agent"), atom(role="lodging_agent"))
    assert (d.resolution, d.policy_rule) == (Resolution.SUPERSEDE, "R1")


def test_r1_lower_authority_is_rejected():
    d = resolve(claim(role="research_agent"), atom(role="budget_agent"))
    assert (d.resolution, d.policy_rule) == (Resolution.REJECT, "R1")


def test_r1_does_not_fire_within_a_tier():
    ctx = rules.build_ctx(claim(role="flight_agent"), atom(role="lodging_agent"),
                          Verdict.CONTRADICTION)
    assert rules.r1_authority(ctx) is None


# --- R2 evidence -----------------------------------------------------------

def test_r2_well_corroborated_existing_rejects_incoming():
    d = resolve(claim(role="ground_agent"), atom(role="ground_agent", evidence=5))
    assert (d.resolution, d.policy_rule) == (Resolution.REJECT, "R2")


def test_r2_does_not_fire_below_margin():
    ctx = rules.build_ctx(claim(role="ground_agent"),
                          atom(role="ground_agent", evidence=2),
                          Verdict.CONTRADICTION)
    assert rules.r2_evidence(ctx) is None


# --- R3 recency ------------------------------------------------------------

def test_r3_materially_more_confident_supersedes():
    d = resolve(claim(role="research_agent", conf=0.8),
                atom(role="research_agent", conf=0.6))
    assert (d.resolution, d.policy_rule) == (Resolution.SUPERSEDE, "R3")


def test_r3_does_not_fire_on_equal_confidence():
    """This is what makes R4 reachable, and S3/S5 contest rather than guess."""
    ctx = rules.build_ctx(claim(role="ground_agent", conf=0.7),
                          atom(role="ground_agent", conf=0.7),
                          Verdict.CONTRADICTION)
    assert rules.r3_recency(ctx) is None


def test_r3_does_not_fire_across_tiers():
    ctx = rules.build_ctx(claim(role="booking_agent", conf=0.9),
                          atom(role="research_agent", conf=0.1),
                          Verdict.CONTRADICTION)
    assert rules.r3_recency(ctx) is None


def test_r3_does_not_fire_when_existing_is_well_evidenced():
    ctx = rules.build_ctx(claim(role="ground_agent", conf=0.9),
                          atom(role="ground_agent", conf=0.5, evidence=3),
                          Verdict.CONTRADICTION)
    assert rules.r3_recency(ctx) is None


# --- R4 contest ------------------------------------------------------------

def test_r4_contests_identical_standing():
    d = resolve(claim(role="ground_agent", conf=0.7),
                atom(role="ground_agent", conf=0.7))
    assert (d.resolution, d.policy_rule) == (Resolution.CONTEST, "R4")


def test_r4_always_matches_so_resolution_is_total():
    ctx = rules.build_ctx(claim(), atom(), Verdict.CONTRADICTION)
    assert rules.r4_contest(ctx) is not None
