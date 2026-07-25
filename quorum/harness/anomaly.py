"""Post-run anomaly detectors, run against the final database state.

These are the numbers the whole submission rests on, so each is a plain SQL
question with an obvious meaning. No mode-specific logic lives here: the same
five queries run against all three modes and the modes are allowed to differ
only in what they produced. [I8]
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass

# Two atoms, same subject_key, both currently valid and active, different
# values. The headline metric: memory that holds two mutually exclusive truths.
CONTRADICTORY_PAIRS_SQL = """
SELECT a.subject_key, a.id, b.id, a.object_text, b.object_text
FROM memory_atom a, memory_atom b
WHERE a.workspace_id = %(ws)s
  AND b.workspace_id = a.workspace_id
  AND a.subject_key  = b.subject_key
  AND a.id < b.id
  AND a.valid_to IS NULL AND b.valid_to IS NULL
  AND a.status = 'active' AND b.status = 'active'
  AND a.object_json::STRING IS DISTINCT FROM b.object_json::STRING
"""

# An action that executed while citing memory which disagrees with the
# scenario's ground truth. This is the user-visible failure: the wrong booking.
EXECUTED_ACTIONS_SQL = """
SELECT id, action_type, payload, required_keys, justifying_atom_ids, gate_result
FROM action_log
WHERE workspace_id = %(ws)s AND run_id = %(run)s AND executed = true
"""

BLOCKED_ACTIONS_SQL = """
SELECT gate_result, count(*) FROM action_log
WHERE workspace_id = %(ws)s AND run_id = %(run)s AND executed = false
GROUP BY gate_result
"""

# An action justified by an atom that had already been superseded when the
# action ran.
STALE_READS_SQL = """
SELECT al.id, al.action_type, ma.id
FROM action_log al, memory_atom ma
WHERE al.workspace_id = %(ws)s AND al.run_id = %(run)s
  AND ma.id = ANY(al.justifying_atom_ids)
  AND ma.valid_to IS NOT NULL
  AND ma.valid_to <= al.created_at
"""

CONTESTED_SQL = """
SELECT count(*) FROM memory_atom
WHERE workspace_id = %(ws)s AND valid_to IS NULL AND status = 'contested'
"""

# An executed action one of whose required keys carries MORE THAN ONE live
# active answer. The agent committed an external effect while its memory held
# two mutually exclusive answers to a question the action depended on.
#
# This is the primary wrongness test, and it is deliberately independent of
# WHICH of the two values the agent happened to pick. An agent that books a
# hotel while memory says both Sep 14 and Sep 15 has made a wrong booking even
# if it guessed the right night -- it had no basis for the guess. Scoring on
# the guess would make the baseline look good or bad by luck; scoring on the
# ambiguity is the property that actually matters.
AMBIGUOUS_ACTIONS_SQL = """
SELECT al.id, al.action_type, k.key, count(DISTINCT ma.object_json::STRING)
FROM action_log al
CROSS JOIN LATERAL unnest(al.required_keys) AS k(key)
JOIN memory_atom ma
  ON ma.workspace_id = al.workspace_id
 AND ma.subject_key = k.key
 AND ma.valid_to IS NULL
 AND ma.status = 'active'
WHERE al.workspace_id = %(ws)s AND al.run_id = %(run)s AND al.executed = true
GROUP BY al.id, al.action_type, k.key
HAVING count(DISTINCT ma.object_json::STRING) > 1
"""

CONFLICT_BREAKDOWN_SQL = """
SELECT detector, verdict, resolution, policy_rule, count(*)
FROM memory_conflict WHERE workspace_id = %(ws)s AND run_id = %(run)s
GROUP BY 1,2,3,4
"""


@dataclass
class Anomalies:
    contradictory_active_pairs: int = 0
    lost_updates: int = 0
    stale_reads: int = 0
    wrong_actions: int = 0
    blocked_actions: int = 0
    contested_atoms: int = 0
    details: dict = None  # type: ignore[assignment]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["details"] = self.details or {}
        return d


def _values_disagree(actual, expected) -> bool:
    if expected is None:
        return False          # ground truth genuinely undecidable -> cannot be "wrong"
    if actual is None:
        return True
    if isinstance(actual, dict) and isinstance(expected, dict):
        return any(str(actual.get(k)) != str(v) for k, v in expected.items())
    return str(actual) != str(expected)


def detect(cur, workspace_id: uuid.UUID, run_id: uuid.UUID, *,
           ground_truth: dict | None = None,
           acknowledged_writes: int | None = None,
           constraints: dict | None = None) -> Anomalies:
    ground_truth = ground_truth or {}
    details: dict = {}

    cur.execute(CONTRADICTORY_PAIRS_SQL, {"ws": workspace_id})
    pairs = cur.fetchall()
    details["contradictory_pairs"] = [
        {"subject_key": r[0], "a": str(r[1]), "b": str(r[2]),
         "a_text": r[3], "b_text": r[4]} for r in pairs
    ]

    cur.execute(CONTESTED_SQL, {"ws": workspace_id})
    contested = cur.fetchone()[0]

    # Wrong actions, by three independent tests. An action is wrong if ANY hold.
    wrong: list[dict] = []
    seen_actions: set[str] = set()

    # (a) executed against a key with more than one live active answer
    cur.execute(AMBIGUOUS_ACTIONS_SQL, {"ws": workspace_id, "run": run_id})
    for aid, atype, key, n in cur.fetchall():
        wrong.append({"action_id": str(aid), "action_type": atype,
                      "subject_key": key, "reason": "ambiguous_memory",
                      "live_answers": n})
        seen_actions.add(f"{aid}:{key}")

    # (b) the value actually acted on disagrees with declared ground truth
    cur.execute(EXECUTED_ACTIONS_SQL, {"ws": workspace_id, "run": run_id})
    executed_rows = cur.fetchall()
    for aid, atype, payload, keys, justifying, gate in executed_rows:
        payload = payload if isinstance(payload, dict) else json.loads(payload or "{}")
        resolved = payload.get("resolved") or {}
        for key, actual in resolved.items():
            if f"{aid}:{key}" in seen_actions:
                continue
            if key in ground_truth and _values_disagree(actual, ground_truth[key]):
                wrong.append({"action_id": str(aid), "action_type": atype,
                              "subject_key": key, "reason": "contradicts_ground_truth",
                              "acted_on": actual, "ground_truth": ground_truth[key]})
                seen_actions.add(f"{aid}:{key}")

    # (c) the payload violates a numeric constraint held in memory
    for aid, atype, payload, keys, justifying, gate in executed_rows:
        payload = payload if isinstance(payload, dict) else json.loads(payload or "{}")
        resolved = payload.get("resolved") or {}
        for field, spec in (constraints or {}).items():
            if field not in payload:
                continue
            bound_atom = resolved.get(spec["key"])
            if not isinstance(bound_atom, dict):
                continue
            bound = bound_atom.get(spec["field"])
            try:
                if bound is not None and float(payload[field]) > float(bound):
                    wrong.append({
                        "action_id": str(aid), "action_type": atype,
                        "subject_key": spec["key"], "reason": "violates_memory_constraint",
                        "acted_on": payload[field], "limit": bound})
            except (TypeError, ValueError):
                continue

    details["wrong_actions"] = wrong

    cur.execute(BLOCKED_ACTIONS_SQL, {"ws": workspace_id, "run": run_id})
    blocked_rows = cur.fetchall()
    details["blocked_by_reason"] = {r[0]: r[1] for r in blocked_rows}
    blocked = sum(r[1] for r in blocked_rows)

    cur.execute(STALE_READS_SQL, {"ws": workspace_id, "run": run_id})
    stale = cur.fetchall()
    details["stale_reads"] = [{"action_id": str(r[0]), "action_type": r[1],
                               "atom_id": str(r[2])} for r in stale]

    # lost updates: writes the memory layer acknowledged that are not in final
    # state at all. Requires the caller to say how many it acknowledged.
    lost = 0
    if acknowledged_writes is not None:
        cur.execute(
            """SELECT count(*) FROM memory_atom
               WHERE workspace_id = %s AND status <> 'rejected'""",
            (workspace_id,))
        present = cur.fetchone()[0]
        lost = max(0, acknowledged_writes - present)
        details["acknowledged_writes"] = acknowledged_writes
        details["atoms_present"] = present

    cur.execute(CONFLICT_BREAKDOWN_SQL, {"ws": workspace_id, "run": run_id})
    details["conflicts"] = [
        {"detector": r[0], "verdict": r[1], "resolution": r[2],
         "policy_rule": r[3], "count": r[4]} for r in cur.fetchall()
    ]

    return Anomalies(
        contradictory_active_pairs=len(pairs),
        lost_updates=lost,
        stale_reads=len(stale),
        wrong_actions=len(wrong),
        blocked_actions=blocked,
        contested_atoms=contested,
        details=details,
    )
