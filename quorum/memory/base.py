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

    def act(self, action: Action) -> GateResult:
        """Execute an action against memory.

        The DEFAULT is ungated, because an action gate is not something you get
        for free from a vector store -- it is Quorum's contribution. A normal
        agent stack recalls what it believes, picks the most recent answer, and
        books. QuorumMemory overrides this with the real gate (§6.5).

        Giving naive and txn_only a gate they would never actually have would
        make the comparison flattering rather than honest.
        """
        return self._act_ungated(action)

    def _resolve_keys(self, cur, action: Action) -> dict[str, list[Atom]]:
        out: dict[str, list[Atom]] = {}
        for key in action.required_keys:
            cur.execute(
                f"""SELECT {ATOM_SELECT} FROM memory_atom
                    WHERE workspace_id = %s AND subject_key = %s
                      AND valid_to IS NULL AND status IN ('active','contested')
                    ORDER BY valid_from DESC""",
                (action.workspace_id, key),
            )
            out[key] = [atom_from_row(r) for r in cur.fetchall()]
        return out

    def _act_ungated(self, action: Action) -> GateResult:
        """What every agent framework does: take what memory says and act.

        With two contradictory 'currently true' atoms it simply picks the most
        recent one and proceeds. Nothing in the stack objects, which is exactly
        how a hotel gets booked for the wrong night.
        """
        with self.pool.connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                resolved = self._resolve_keys(cur, action)
                missing = [k for k, atoms in resolved.items() if not atoms]
                if missing:
                    result = GateResult(
                        Gate.BLOCKED_MISSING, (), False,
                        f"no memory for {', '.join(missing)}", tuple(missing),
                        f"no memory for {', '.join(missing)}")
                else:
                    chosen = {k: atoms[0] for k, atoms in resolved.items()}
                    result = GateResult(
                        Gate.ALLOWED,
                        tuple(a.id for a in chosen.values()),
                        True, "executed")
                self._log_action(cur, action, result, resolved)
        return result

    def _act_gated(self, action: Action) -> GateResult:
        """The action gate: turn memory consistency into a safety property.

        Refusing to act is a feature. [I5]
        """
        blocked: list[str] = []
        reason = None
        gate = Gate.ALLOWED
        justifying: list[uuid.UUID] = []

        with self.pool.connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                resolved = self._resolve_keys(cur, action)
                for key, atoms in resolved.items():
                    if not atoms:
                        gate, reason = Gate.BLOCKED_MISSING, f"no memory for {key}"
                        blocked.append(key)
                        continue
                    if any(a.status == Status.CONTESTED for a in atoms):
                        gate = Gate.BLOCKED_CONTESTED
                        reason = (f"{key} is contested: "
                                  + " vs ".join(sorted(a.object_text for a in atoms)))
                        blocked.append(key)
                        continue
                    active = [a for a in atoms if a.status == Status.ACTIVE]
                    if len(active) > 1:
                        gate = Gate.BLOCKED_AMBIGUOUS
                        reason = (f"{key} has {len(active)} conflicting active atoms: "
                                  + " vs ".join(sorted(a.object_text for a in active)))
                        blocked.append(key)
                        continue
                    justifying.extend(a.id for a in active)

                executed = gate == Gate.ALLOWED
                result = GateResult(
                    gate_result=gate,
                    justifying_atom_ids=tuple(justifying) if executed else (),
                    executed=executed,
                    outcome="executed" if executed else reason,
                    blocked_keys=tuple(blocked),
                    reason=reason,
                )
                self._log_action(cur, action, result, resolved)
        return result

    def _log_action(self, cur, action: Action, result: GateResult,
                    resolved: dict[str, list[Atom]]) -> None:
        """Record the action AND the memory it was derived from.

        payload.resolved is what makes wrong_actions measurable: it is the
        value the agent actually acted on, which the anomaly detector compares
        against the scenario's ground truth.
        """
        payload = dict(action.payload)
        if result.executed:
            payload["resolved"] = {
                k: (atoms[0].object_json if atoms[0].object_json is not None
                    else atoms[0].object_text)
                for k, atoms in resolved.items() if atoms
            }
        cur.execute(INSERT_ACTION_SQL, {
            "ws": action.workspace_id, "run_id": self.run_id,
            "agent": action.agent_id, "type": action.action_type,
            "payload": json.dumps(payload, default=str),
            "keys": list(action.required_keys), "gate": result.gate_result,
            "justifying": [str(i) for i in result.justifying_atom_ids],
            "executed": result.executed, "outcome": result.outcome,
        })

    # -- helpers shared by every mode -----------------------------------
    def registry(self) -> dict[str, int]:
        if self._registry is None:
            with self.pool.connection() as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    self._registry = load_registry(cur)
        return self._registry

    @staticmethod
    def _visible_to(atom: Atom, agent: AgentCtx) -> bool:
        """Reads are scoped. A sub-agent does not inherit full memory. [I7]"""
        if atom.visibility == "workspace":
            return True
        if atom.visibility == "role":
            return atom.writer_role == agent.role or "workspace" in agent.visibility_scopes
        return atom.writer_agent_id == agent.agent_id      # private
