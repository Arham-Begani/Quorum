"""Scenario vocabulary.

A scenario is a deterministic sequence of agent turns plus an injected
contradiction, and the expected outcome per mode so tests can assert the
DIVERGENCE rather than just "it ran". If txn_only ever matches quorum on
S1-S5, the test must fail loudly. (CLAUDE.md §2)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RememberTurn:
    agent_id: str
    subject_key: str
    predicate: str
    object_text: str
    object_json: dict | None
    confidence: float = 0.6
    # Turns sharing a non-None group are dispatched SIMULTANEOUSLY against a
    # barrier. This is what makes S5 a real race rather than a simulated one.
    concurrent_group: str | None = None
    label: str = ""


@dataclass(frozen=True)
class ActTurn:
    agent_id: str
    action_type: str
    payload: dict
    required_keys: tuple[str, ...]
    label: str = ""


Turn = RememberTurn | ActTurn


@dataclass(frozen=True)
class Expectation:
    """What each mode should do. Asserted by tests/scenarios."""

    contradictory_active_pairs: str = "0"   # "0" | ">0"
    wrong_actions: str = "0"
    blocked_actions: str = "0"
    note: str = ""


@dataclass(frozen=True)
class ScenarioPlan:
    id: str
    title: str
    description: str
    tier: str                       # "tier1" | "tier2"
    turns: tuple[Turn, ...]
    ground_truth: dict              # subject_key -> the value that is actually correct
    expectations: dict              # mode -> Expectation
    wrong_action_note: str = ""
    # payload field -> {"key": subject_key, "field": json field} — the action's
    # payload value must not exceed the value memory holds under that key.
    constraints: dict = field(default_factory=dict)
    # True when the conflicting claims do NOT share a subject_key, so the ONLY
    # thing that can surface them as a candidate pair is ANN over a real
    # semantic embedding space. The synthetic offline embedder places distinct
    # subject keys near-orthogonal by construction and cannot do this. Such a
    # scenario is expected to FAIL without Bedrock Titan, and that failure is
    # the cleanest evidence that the vector index is load-bearing rather than
    # decorative.
    requires_semantic_embeddings: bool = False

    @property
    def subject_keys(self) -> tuple[str, ...]:
        return tuple({t.subject_key for t in self.turns
                      if isinstance(t, RememberTurn)})


def check(expected: str, actual: int) -> bool:
    expected = expected.strip()
    if expected.startswith(">="):
        return actual >= int(expected[2:])
    if expected.startswith(">"):
        return actual > int(expected[1:])
    if expected.startswith("<="):
        return actual <= int(expected[2:])
    if expected.startswith("<"):
        return actual < int(expected[1:])
    return actual == int(expected)
