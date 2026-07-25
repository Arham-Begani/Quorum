# Auditor queries — rehearse these before recording

An unrehearsed live MCP query on camera is a coin flip. These are the questions,
in order, with the SQL each should produce and what to say while it runs.

---

## 1. "Show me every contested memory in this workspace and the action it blocked."

The headline. Contested memory plus the consequence, in one row.

```sql
SELECT a.subject_key,
       a.object_text,
       a.writer_role,
       a.status,
       l.action_type,
       l.gate_result,
       l.outcome
FROM memory_atom a
JOIN action_log l
  ON l.workspace_id = a.workspace_id
 AND a.subject_key = ANY(l.required_keys)
WHERE a.status = 'contested'
  AND a.valid_to IS NULL
  AND l.executed = false
ORDER BY l.created_at DESC;
```

> "Two agents claimed the same transfer slot. The policy engine could not
> justify picking either one, so it marked both contested — and the booking that
> depended on that slot was refused. The system declined to guess."

---

## 2. "Which agent wrote the atom that justified booking X?"

Provenance from action back to the exact memory that caused it.

```sql
SELECT l.action_type,
       l.payload,
       a.object_text,
       a.writer_agent_id,
       a.writer_role,
       a.confidence,
       a.valid_from,
       a.valid_to
FROM action_log l
JOIN memory_atom a ON a.id = ANY(l.justifying_atom_ids)
WHERE l.run_id = '<RUN_ID>'
ORDER BY l.created_at;
```

> "This booking was made because of exactly these atoms — and in the naive run,
> here is the one that was wrong."

---

## 3. "Show the same scenario across all three modes."

The comparison, straight from the database rather than the dashboard.

```sql
SELECT mode,
       report->'anomalies'->>'contradictory_active_pairs' AS contradictory,
       report->'anomalies'->>'wrong_actions'              AS wrong_actions,
       report->'anomalies'->>'blocked_actions'            AS blocked,
       report->'performance'->>'txn_retries'              AS retries
FROM run
WHERE scenario = 'S5_concurrent_race' AND report IS NOT NULL
ORDER BY started_at DESC
LIMIT 3;
```

---

## 4. "What did memory look like before the booking?"

The forensic read. Requires `gc.ttlseconds` to have been raised at provisioning.

```sql
SELECT subject_key, object_text, writer_role, status
FROM memory_atom
AS OF SYSTEM TIME '-30s'
WHERE workspace_id = '<WORKSPACE_ID>'
ORDER BY valid_from;
```

> "That is not a log we kept. That is the database answering what it actually
> held at that instant, because memory is append-only and the GC window was
> raised on day one."

---
