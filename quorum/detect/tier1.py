"""Tier 1 — structural contradiction detection.

Deterministic, ~0ms, no network, no cost, no LLM. This is the workhorse: if
tier 1 is doing its job, tier 2 fires rarely and the system is demonstrably not
"just ask an LLM". If tier 2 starts firing on everything, the subject_key
normalization is broken, not the classifier. (CLAUDE.md §15.7)

MUST stay pure — it runs INSIDE the serializable transaction, where a network
call would violate I1 and collapse throughput under contention.

    classify(incoming, existing) -> Verdict | None

None means inconclusive: escalate to tier 2 (which only ever runs in Phase A,
outside the transaction).
"""

from __future__ import annotations

from ..memory.schema import Atom, Claim, Verdict
from .coerce import point_in_range, range_of, scalar_of, values_equal

EQUALS = "equals"
FORBIDS = "forbids"
PREFERS = "prefers"


def classify(incoming: Claim, existing: Atom) -> str | None:
    """Return a Verdict, or None if tier 1 cannot decide."""
    # Different attributes entirely -> tier 1 has no structural opinion. The
    # semantic layer may still find a relationship; that is tier 2's job.
    if incoming.subject_key != existing.subject_key:
        return None

    inc_json, exi_json = incoming.object_json, existing.object_json

    # forbids vs equals on the same key with an overlapping value.
    # Checked before the scalar path because a `forbids` claim carries a value
    # but does not assert it — it denies it.
    fv = _forbids_conflict(incoming, existing)
    if fv is not None:
        return fv

    inc_scalar = scalar_of(inc_json)
    exi_scalar = scalar_of(exi_json)

    # One side has no parseable structure -> cannot compare structurally.
    if inc_json is None or exi_json is None:
        # A concrete value arriving where there was none is a refinement.
        if exi_json is None and inc_json is not None and incoming.predicate == existing.predicate:
            return Verdict.REFINEMENT
        return None

    # Range/point refinement: existing says 1000-3000, incoming says 2400.
    exi_range, inc_range = range_of(exi_json), range_of(inc_json)
    if exi_range is not None and inc_scalar is not None:
        return Verdict.REFINEMENT if point_in_range(inc_scalar, exi_range) else Verdict.CONTRADICTION
    if inc_range is not None and exi_scalar is not None:
        # Incoming is broader than what we already know: not new information,
        # and not a conflict either.
        return Verdict.AGREEMENT if point_in_range(exi_scalar, inc_range) else Verdict.CONTRADICTION

    if inc_scalar is None or exi_scalar is None:
        # Both structured but neither reduces to a scalar -> compare wholesale.
        if inc_json == exi_json:
            return Verdict.AGREEMENT
        return None

    if values_equal(inc_scalar, exi_scalar):
        return Verdict.AGREEMENT

    # Unequal scalars. Only a contradiction if both sides actually ASSERT the
    # value. Two `prefers` claims with different values can coexist (someone
    # can prefer two things); two `equals` claims cannot.
    if incoming.predicate == EQUALS and existing.predicate == EQUALS:
        return Verdict.CONTRADICTION

    return None


def _forbids_conflict(incoming: Claim, existing: Atom) -> str | None:
    inc_forbids = incoming.predicate == FORBIDS
    exi_forbids = existing.predicate == FORBIDS
    if inc_forbids == exi_forbids:
        return None  # both forbid, or neither does -> not this branch

    forbidden = scalar_of(incoming.object_json if inc_forbids else existing.object_json)
    asserted = scalar_of(existing.object_json if inc_forbids else incoming.object_json)
    asserting_pred = existing.predicate if inc_forbids else incoming.predicate

    if forbidden is None or asserted is None:
        return None
    if asserting_pred not in (EQUALS, PREFERS):
        return None
    if values_equal(forbidden, asserted):
        return Verdict.CONTRADICTION
    # Forbidding X while asserting Y is perfectly consistent.
    return Verdict.UNRELATED


def is_structural_pair(incoming: Claim, existing: Atom) -> bool:
    """Can tier 1 even have an opinion about this pair?"""
    return incoming.subject_key == existing.subject_key
