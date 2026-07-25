"""Ordered evaluation of the resolution rules.

resolve() takes the verdicts for every conflicting neighbour and returns ONE
decision for the write, plus the per-pair records that go into memory_conflict.

When several neighbours conflict, the most severe outcome wins: a single
CONTEST anywhere makes the whole write contested, because letting a write
partially commit against a contested fact is exactly the hole the action gate
exists to close.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..memory.schema import Atom, Claim, ConflictRecord, Resolution, Verdict
from . import rules
from .rules import Decision, build_ctx, short_circuit

# Most severe first. Used to pick a single write-level outcome.
SEVERITY = {
    Resolution.CONTEST: 5,
    Resolution.REJECT: 4,
    Resolution.SUPERSEDE: 3,
    Resolution.REINFORCE: 2,
    Resolution.ACCEPT: 1,
}


@dataclass(frozen=True)
class PairOutcome:
    existing: Atom
    verdict: str
    decision: Decision
    detector: str
    similarity: float | None = None
    adjudicator_ms: int | None = None


@dataclass(frozen=True)
class ResolutionPlan:
    resolution: str
    policy_rule: str | None
    rationale: str | None
    supersede_ids: tuple = ()
    reinforce_ids: tuple = ()
    contest_ids: tuple = ()
    pairs: tuple[PairOutcome, ...] = ()

    def conflict_records(self, incoming_atom_id=None) -> list[ConflictRecord]:
        return [
            ConflictRecord(
                existing_atom_id=p.existing.id,
                incoming_atom_id=incoming_atom_id,
                subject_key=p.existing.subject_key,
                detector=p.detector,
                verdict=p.verdict,
                resolution=p.decision.resolution,
                similarity=p.similarity,
                policy_rule=p.decision.policy_rule,
                rationale=p.decision.rationale,
                adjudicator_ms=p.adjudicator_ms,
            )
            for p in self.pairs
        ]


def resolve_pair(incoming: Claim, existing: Atom, verdict: str,
                 registry: dict[str, int] | None = None) -> Decision:
    """Apply the ordered rules to one pair. R4 always matches, so this is total."""
    ctx = build_ctx(incoming, existing, verdict, registry)
    sc = short_circuit(ctx)
    if sc is not None:
        return sc
    for rule in rules.ORDERED_RULES:
        decision = rule(ctx)
        if decision is not None:
            return decision
    # Unreachable: r4_contest always returns a Decision.
    return rules.r4_contest(ctx)


def resolve(
    incoming: Claim,
    pairs: list[tuple[Atom, str, str, float | None, int | None]],
    registry: dict[str, int] | None = None,
) -> ResolutionPlan:
    """pairs: [(existing_atom, verdict, detector, similarity, adjudicator_ms)]"""
    if not pairs:
        return ResolutionPlan(Resolution.ACCEPT, None,
                              "No conflicting neighbour found.", pairs=())

    outcomes: list[PairOutcome] = []
    for existing, verdict, detector, similarity, adj_ms in pairs:
        decision = resolve_pair(incoming, existing, verdict, registry)
        outcomes.append(PairOutcome(existing, verdict, decision, detector,
                                    similarity, adj_ms))

    winner = max(outcomes, key=lambda o: SEVERITY[o.decision.resolution])
    resolution = winner.decision.resolution

    supersede = tuple(o.existing.id for o in outcomes
                      if o.decision.resolution == Resolution.SUPERSEDE)
    reinforce = tuple(o.existing.id for o in outcomes
                      if o.decision.resolution == Resolution.REINFORCE)
    contest = tuple(o.existing.id for o in outcomes
                    if o.decision.resolution == Resolution.CONTEST)

    # A CONTEST anywhere poisons the whole write: we cannot supersede on the
    # authority of one neighbour while another says the fact is disputed.
    if resolution == Resolution.CONTEST:
        supersede = ()
        reinforce = ()

    return ResolutionPlan(
        resolution=resolution,
        policy_rule=winner.decision.policy_rule,
        rationale=winner.decision.rationale,
        supersede_ids=supersede,
        reinforce_ids=reinforce,
        contest_ids=contest,
        pairs=tuple(outcomes),
    )
