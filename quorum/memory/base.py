"""MemoryClient — the interface all three modes implement.

The three modes differ ONLY in this object. Same workload driver, same seed,
same agent implementation, same scenarios. If the comparison ever needs an
`if mode ==` outside factory.py, the comparison has stopped being honest. [I8]

Shared machinery lives here so naive/txn_only/quorum cannot accidentally drift
in the parts that are supposed to be identical (the SQL shapes, the recall
path, the action gate). What differs is deliberately overridden.
"""

from __future__ import annotations

import json
import uuid
from abc import ABC, abstractmethod
from datetime import datetime

from ..db.metrics import metrics
from ..policy.tiers import load_registry
from .schema import (
    ATOM_SELECT,
    Action,
    Atom,
    AgentCtx,
    Claim,
    Gate,
    GateResult,
    RememberResult,
    Status,
    atom_from_row,
)

INSERT_ATOM_SQL = f"""
INSERT INTO memory_atom
  (id, workspace_id, subject_key, predicate, object_text, object_json,
   embedding, writer_agent_id, writer_role, confidence, evidence_count,
   status, visibility, run_id)
VALUES
  (%(id)s, %(ws)s, %(sk)s, %(pred)s, %(text)s, %(json)s::JSONB,
   %(vec)s::VECTOR, %(agent)s, %(role)s, %(conf)s, %(evidence)s,
   %(status)s, %(visibility)s, %(run_id)s)
"""

SUPERSEDE_SQL = """
UPDATE memory_atom
   SET valid_to = now(), superseded_by = %(new_id)s, status = 'superseded'
 WHERE id = ANY(%(ids)s::UUID[]) AND valid_to IS NULL
"""

CONTEST_SQL = """
UPDATE memory_atom SET status = 'contested'
 WHERE id = ANY(%(ids)s::UUID[]) AND valid_to IS NULL
"""

REINFORCE_SQL = """
UPDATE memory_atom
   SET evidence_count = evidence_count + 1,
       confidence = greatest(confidence, %(conf)s)
 WHERE id = ANY(%(ids)s::UUID[]) AND valid_to IS NULL
"""

INSERT_CONFLICT_SQL = """
INSERT INTO memory_conflict
  (workspace_id, run_id, incoming_atom_id, existing_atom_id, subject_key,
   detector, similarity, verdict, resolution, policy_rule, rationale, adjudicator_ms)
VALUES (%(ws)s, %(run_id)s, %(incoming)s, %(existing)s, %(sk)s, %(detector)s,
        %(similarity)s, %(verdict)s, %(resolution)s, %(rule)s, %(rationale)s, %(adj_ms)s)
"""

INSERT_ACTION_SQL = """
INSERT INTO action_log
  (workspace_id, run_id, agent_id, action_type, payload, required_keys,
   gate_result, justifying_atom_ids, executed, outcome)
VALUES (%(ws)s, %(run_id)s, %(agent)s, %(type)s, %(payload)s::JSONB, %(keys)s,
        %(gate)s, %(justifying)s::UUID[], %(executed)s, %(outcome)s)
"""


def vector_literal(vec) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


class MemoryClient(ABC):
    """remember / recall / act."""

    mode: str = "abstract"

    # Declared capabilities. Consumers ask the client what it can do rather
    # than asking which mode it is -- that is what keeps mode branching out of
    # the driver, the reporter and the dashboard. [I8]
    uses_semantic_layer: bool = False
    uses_transactions: bool = False
    has_action_gate: bool = False

    def __init__(self, pool, embedder, cfg=None):
        self.pool = pool
        self.embedder = embedder
        self.cfg = cfg or {}
        self.run_id: uuid.UUID | None = self.cfg.get("run_id")
        self.ann_k = int(self.cfg.get("ann_k", 8))
        self._registry: dict[str, int] | None = None

    # -- interface ------------------------------------------------------
    @abstractmethod
    def remember(self, claim: Claim) -> RememberResult: ...

    def recall(
        self,
        query: str,
        *,
        agent: AgentCtx,
        workspace_id: uuid.UUID,
        subject_keys: list[str] | None = None,
        as_of: datetime | None = None,
        limit: int | None = None,
    ) -> list[Atom]:
        """Hybrid retrieval: exact subject keys UNION semantic neighbourhood.

        Identical in all three modes -- the modes differ in what they WRITE,
        not in how they read. Contested atoms are returned and flagged, never
        dropped silently. [I5]
        """
        limit = limit or self.ann_k
        aost = ""
        if as_of is not None:
            # Parameterising a timestamp into AS OF SYSTEM TIME is not allowed;
            # the value is a datetime we format ourselves, never user text.
            aost = f"AS OF SYSTEM TIME '{as_of.astimezone().isoformat()}'"

        vec = vector_literal(self.embedder.embed(query)) if query else None
        rows: dict = {}

        with self.pool.connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                if subject_keys:
                    cur.execute(
                        f"""SELECT {ATOM_SELECT} FROM memory_atom {aost}
                            WHERE workspace_id = %s AND valid_to IS NULL
                              AND subject_key = ANY(%s)""",
                        (workspace_id, list(subject_keys)),
                    )
                    for row in cur.fetchall():
                        rows[row[0]] = atom_from_row(row)
                if vec is not None:
                    cur.execute(
                        f"""SELECT {ATOM_SELECT}, embedding <-> %s::VECTOR AS distance
                            FROM memory_atom {aost}
                            WHERE workspace_id = %s AND valid_to IS NULL
                            ORDER BY embedding <-> %s::VECTOR LIMIT %s""",
                        (vec, workspace_id, vec, limit),
                    )
                    for row in cur.fetchall():
                        atom = atom_from_row(row, with_distance=True)
                        rows.setdefault(atom.id, atom)

        visible = [a for a in rows.values() if self._visible_to(a, agent)]
        visible.sort(key=lambda a: (a.distance if a.distance is not None else 1e9))
        return visible
