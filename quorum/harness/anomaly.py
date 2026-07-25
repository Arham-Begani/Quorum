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
