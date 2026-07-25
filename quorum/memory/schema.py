"""Row <-> dataclass mapping and the vocabulary the whole system speaks.

A *verdict* is the classifier's judgement about a PAIR of claims.
A *resolution* is the policy engine's decision about the WRITE.
Keeping those two words distinct is what keeps §6.2 and §6.3 separable.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any


class Verdict:
    AGREEMENT = "agreement"
    REFINEMENT = "refinement"
    CONTRADICTION = "contradiction"
    UNRELATED = "unrelated"
    ALL = (AGREEMENT, REFINEMENT, CONTRADICTION, UNRELATED)


class Resolution:
    ACCEPT = "accept"
    SUPERSEDE = "supersede"
    REINFORCE = "reinforce"
    REJECT = "reject"
    CONTEST = "contest"
    ALL = (ACCEPT, SUPERSEDE, REINFORCE, REJECT, CONTEST)


class Status:
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    CONTESTED = "contested"
    REJECTED = "rejected"


class Detector:
    TIER1 = "tier1_structural"
    TIER2 = "tier2_semantic"


class Gate:
    ALLOWED = "allowed"
    BLOCKED_CONTESTED = "blocked_contested"
    BLOCKED_MISSING = "blocked_missing"
    BLOCKED_AMBIGUOUS = "blocked_ambiguous"


@dataclass(frozen=True)
class AgentCtx:
    """Who is acting, and what they are allowed to see."""

    agent_id: str
    role: str
    authority_tier: int
    visibility_scopes: tuple[str, ...] = ("workspace",)


@dataclass(frozen=True)
class Claim:
    """An incoming assertion, before it is anything in the database."""

    workspace_id: uuid.UUID
    subject_key: str
    predicate: str
    object_text: str
    object_json: dict | None
    agent_id: str
    role: str
    confidence: float = 0.6
    visibility: str = "workspace"

    def embed_text(self) -> str:
        """What gets embedded. Stable across writers so the same fact about the
        same attribute lands in the same neighbourhood."""
        return f"{self.subject_key} {self.predicate} {self.object_text}"


@dataclass(frozen=True)
class Atom:
    """A stored memory row."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    subject_key: str
    predicate: str
    object_text: str
    object_json: dict | None
    writer_agent_id: str
    writer_role: str
    confidence: float
    evidence_count: int
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    superseded_by: uuid.UUID | None = None
    status: str = Status.ACTIVE
    visibility: str = "workspace"
    distance: float | None = None    # populated by ANN searches

    @property
    def is_live(self) -> bool:
        return self.valid_to is None

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "workspace_id": str(self.workspace_id),
            "subject_key": self.subject_key,
            "predicate": self.predicate,
            "object_text": self.object_text,
            "object_json": self.object_json,
            "writer_agent_id": self.writer_agent_id,
            "writer_role": self.writer_role,
            "confidence": self.confidence,
            "evidence_count": self.evidence_count,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "superseded_by": str(self.superseded_by) if self.superseded_by else None,
            "status": self.status,
            "visibility": self.visibility,
            "distance": self.distance,
        }


# The column list every neighbourhood/recall query selects, in order.
ATOM_COLUMNS = (
    "id", "workspace_id", "subject_key", "predicate", "object_text", "object_json",
    "writer_agent_id", "writer_role", "confidence", "evidence_count",
    "valid_from", "valid_to", "superseded_by", "status", "visibility",
)
ATOM_SELECT = ", ".join(ATOM_COLUMNS)


def atom_from_row(row: tuple, *, with_distance: bool = False) -> Atom:
    n = len(ATOM_COLUMNS)
    vals = row[:n]
    distance = row[n] if with_distance and len(row) > n else None
    return Atom(*vals, distance=distance)  # type: ignore[arg-type]


@dataclass(frozen=True)
class ConflictRecord:
    """One detection. Written to memory_conflict whether or not it was a problem."""

    existing_atom_id: uuid.UUID
    subject_key: str
    detector: str
    verdict: str
    resolution: str
    similarity: float | None = None
    policy_rule: str | None = None
    rationale: str | None = None
    adjudicator_ms: int | None = None
    incoming_atom_id: uuid.UUID | None = None

    def to_dict(self) -> dict:
        return {
            "existing_atom_id": str(self.existing_atom_id),
            "incoming_atom_id": str(self.incoming_atom_id) if self.incoming_atom_id else None,
            "subject_key": self.subject_key,
            "detector": self.detector,
            "verdict": self.verdict,
            "resolution": self.resolution,
            "similarity": self.similarity,
            "policy_rule": self.policy_rule,
            "rationale": self.rationale,
            "adjudicator_ms": self.adjudicator_ms,
        }


@dataclass(frozen=True)
class RememberResult:
    atom_id: uuid.UUID | None
    resolution: str
    policy_rule: str | None = None
    conflicts: tuple[ConflictRecord, ...] = ()
    retries: int = 0
    latency_ms: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "atom_id": str(self.atom_id) if self.atom_id else None,
            "resolution": self.resolution,
            "policy_rule": self.policy_rule,
            "conflicts": [c.to_dict() for c in self.conflicts],
            "retries": self.retries,
            "latency_ms": round(self.latency_ms, 1),
            "error": self.error,
        }


@dataclass(frozen=True)
class Action:
    """Something with an external effect. Everything an agent actually DOES."""

    workspace_id: uuid.UUID
    agent_id: str
    action_type: str
    payload: dict
    required_keys: tuple[str, ...]


@dataclass(frozen=True)
class GateResult:
    gate_result: str
    justifying_atom_ids: tuple[uuid.UUID, ...] = ()
    executed: bool = False
    outcome: str | None = None
    blocked_keys: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def allowed(self) -> bool:
        return self.gate_result == Gate.ALLOWED

    def to_dict(self) -> dict:
        return {
            "gate_result": self.gate_result,
            "justifying_atom_ids": [str(i) for i in self.justifying_atom_ids],
            "executed": self.executed,
            "outcome": self.outcome,
            "blocked_keys": list(self.blocked_keys),
            "reason": self.reason,
        }


def json_or_none(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True)
