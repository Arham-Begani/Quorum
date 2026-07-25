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
