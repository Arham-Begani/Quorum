"""Resolution rules R1-R4. Ordered, first match wins. (CLAUDE.md §6.3)

Each rule is a pure function `(ctx) -> Decision | None`. Pure means trivially
unit-testable and easy to put on a slide, which is the entire reason the
resolution logic is rules and not an LLM: it is explainable. An LLM that merges
contradictory facts is unreliable, unexplainable, and destroys the CONTEST
beat, which is the best safety story this project has. (CLAUDE.md §12)

R4 is not a failure mode. It is the safety net: when the system cannot justify
picking a winner, it declines to guess and escalates to a human by marking both
atoms contested and blocking dependent actions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..memory.schema import Atom, Claim, Resolution, Verdict
from .tiers import tier_of

EVIDENCE_MARGIN = int(os.environ.get("EVIDENCE_MARGIN", 2))
CONF_EPSILON = float(os.environ.get("CONF_EPSILON", 0.05))


@dataclass(frozen=True)
class Decision:
    resolution: str
    policy_rule: str
    rationale: str


@dataclass(frozen=True)
class RuleCtx:
    incoming: Claim
    existing: Atom
    verdict: str
    incoming_tier: int
    existing_tier: int
    evidence_margin: int = EVIDENCE_MARGIN
    conf_epsilon: float = CONF_EPSILON


def build_ctx(incoming: Claim, existing: Atom, verdict: str,
              registry: dict[str, int] | None = None) -> RuleCtx:
    return RuleCtx(
        incoming=incoming, existing=existing, verdict=verdict,
        incoming_tier=tier_of(incoming.role, registry),
        existing_tier=tier_of(existing.writer_role, registry),
    )


# --- short-circuits on the verdict itself ---------------------------------

def short_circuit(ctx: RuleCtx) -> Decision | None:
    """Non-contradiction verdicts never reach R1-R4."""
    if ctx.verdict == Verdict.AGREEMENT:
        return Decision(Resolution.REINFORCE, "agreement",
                        "Both claims assert the same value; corroboration recorded.")
    if ctx.verdict == Verdict.REFINEMENT:
        return Decision(Resolution.SUPERSEDE, "refinement",
                        "Incoming claim is strictly more specific than the existing one.")
    if ctx.verdict == Verdict.UNRELATED:
        return Decision(Resolution.ACCEPT, "unrelated",
                        "Claims concern different facts; both can stand.")
    return None


# --- R1..R4 ----------------------------------------------------------------

def r1_authority(ctx: RuleCtx) -> Decision | None:
    if ctx.incoming_tier < ctx.existing_tier:
        return Decision(
            Resolution.SUPERSEDE, "R1",
            f"Incoming writer {ctx.incoming.role} (tier {ctx.incoming_tier}) outranks "
            f"{ctx.existing.writer_role} (tier {ctx.existing_tier}).")
    if ctx.incoming_tier > ctx.existing_tier:
        return Decision(
            Resolution.REJECT, "R1",
            f"Existing writer {ctx.existing.writer_role} (tier {ctx.existing_tier}) outranks "
            f"{ctx.incoming.role} (tier {ctx.incoming_tier}); incoming claim rejected.")
    return None


def r2_evidence(ctx: RuleCtx) -> Decision | None:
    incoming_evidence = 1                       # a fresh claim has one witness
    delta = incoming_evidence - ctx.existing.evidence_count
    if abs(delta) < ctx.evidence_margin:
        return None
    if delta > 0:
        return Decision(Resolution.SUPERSEDE, "R2",
                        f"Incoming claim has {incoming_evidence} corroborations vs "
                        f"{ctx.existing.evidence_count}.")
    return Decision(Resolution.REJECT, "R2",
                    f"Existing claim has {ctx.existing.evidence_count} corroborations vs "
                    f"{incoming_evidence}; incoming rejected.")


def r3_recency(ctx: RuleCtx) -> Decision | None:
    """Newer wins within a tier -- but only on a STRICTLY better claim.

    DELIBERATE DEVIATION from the literal formula in CLAUDE.md §6.3, which
    reads `confidence_incoming >= confidence_existing - CONF_EPSILON`. That
    version fires whenever confidences are equal, which makes R4 unreachable
    for two same-tier writers with identical evidence and confidence -- exactly
    the S3 case that §8 says must resolve to CONTEST, and exactly the "tune the
    thresholds so at least one scenario lands in R4" instruction in §6.3.

    Requiring the newer claim to be MORE confident by more than CONF_EPSILON
    satisfies both: S4 (0.80 vs 0.60) supersedes on recency, S3 (0.70 vs 0.70)
    falls through to CONTEST. Recorded in docs/CONSISTENCY_MODEL.md.
    """
    if ctx.incoming_tier != ctx.existing_tier:
        return None
    if ctx.existing.evidence_count >= ctx.evidence_margin:
        return None                              # well-corroborated: not "low evidence"
    if ctx.incoming.confidence > ctx.existing.confidence + ctx.conf_epsilon:
        return Decision(
            Resolution.SUPERSEDE, "R3",
            f"Same authority tier ({ctx.incoming_tier}), low evidence on both, and the "
            f"newer claim is materially more confident "
            f"({ctx.incoming.confidence:.2f} vs {ctx.existing.confidence:.2f}).")
    return None


def r4_contest(ctx: RuleCtx) -> Decision | None:
    return Decision(
        Resolution.CONTEST, "R4",
        f"Cannot adjudicate: {ctx.incoming.role} and {ctx.existing.writer_role} are both "
        f"tier {ctx.incoming_tier} with comparable evidence and confidence. "
        "Both atoms marked contested; dependent actions blocked.")


ORDERED_RULES = (r1_authority, r2_evidence, r3_recency, r4_contest)
