# Architecture

```
                        ┌───────────────────────────────┐
                        │   Atlas Travel Swarm (Lambda) │
                        │  flights · lodging · ground   │
                        │       · budget · booking      │
                        └───────────────┬───────────────┘
                                        │ remember() / recall() / act()
                        ┌───────────────▼───────────────┐
                        │      Quorum Memory Client      │
                        │  (naive | txn_only | quorum)   │
                        │   injected by factory.py only  │
                        └───────────────┬───────────────┘
                                        │
             ┌──────────────────────────┼──────────────────────────┐
   ┌─────────▼─────────┐   ┌────────────▼───────────┐  ┌───────────▼──────────┐
   │  Phase A          │   │  Phase B  (ONE TXN)    │  │    Action gate       │
   │  (outside txn)    │   │                        │  │                      │
   │ · embed claim     │   │ · ANN neighbourhood    │  │ · require subject    │
   │ · probe neighbours│   │   (authoritative)      │  │   keys uncontested   │
   │ · tier-1 classify │   │ · reconcile vs probe   │  │ · else BLOCK         │
   │ · tier-2 adjudicate│  │ · policy resolve       │  │ · emit action_log    │
   │   (bounded)       │   │ · insert + supersede   │  │                      │
   │ · PreparedDecision│   │ · log conflicts        │  │  (quorum mode only)  │
   └───────────────────┘   └────────────┬───────────┘  └──────────────────────┘
              ▲                         │
              └── re-prepare, bounded ──┘  (new unjudgeable neighbour)
                                        │
                        ┌───────────────▼───────────────┐
                        │        CockroachDB Cloud       │
                        │  memory_atom (+ VECTOR index)  │
                        │  memory_conflict · action_log  │
                        │  agent_registry · run          │
                        └───────────────┬───────────────┘
                                        │
             ┌──────────────────────────┼──────────────────────────┐
   ┌─────────▼─────────┐   ┌────────────▼───────────┐  ┌───────────▼──────────┐
   │ Managed MCP Server│   │   Dashboard (Next.js)  │  │     ccloud CLI       │
   │ read-only auditor │   │  3-mode split screen   │  │ provisioning · RBAC  │
   │ via Claude Code   │   │  conflicts · health    │  │ 4 roles · audit log  │
   └───────────────────┘   │  forensic timeline     │  └──────────────────────┘
                           └────────────────────────┘

AWS: Bedrock (Titan embeddings + Claude adjudicator) · Lambda (swarm fan-out)
     S3 (run reports) · CloudWatch (retry + contradiction metrics)
```

## The write path in detail

```
remember(claim)

  PHASE A — no transaction open, no locks held                          [I1]
  ─────────────────────────────────────────────────────────────────────
   1. normalize subject_key                        quorum/memory/keys.py
   2. embed(subject_key + predicate + object_text) quorum/embed/bedrock.py
   3. probe neighbourhood (own read-only txn, may be stale)
   4. for each neighbour:
        tier-1 structural classify                 quorum/detect/tier1.py
        if inconclusive AND similarity >= tau:
            tier-2 LLM adjudicate (bounded)        quorum/detect/tier2.py
   5. build PreparedDecision{embedding, verdicts, probe_ids}

  PHASE B — one SERIALIZABLE transaction via run_txn                    [I2,I3]
  ─────────────────────────────────────────────────────────────────────
   1. authoritative neighbourhood read (ANN UNION exact subject_key)
   2. for neighbours NOT in the probe (a concurrent writer landed):
        tier-1 only — deterministic, no network                        [I1]
        if tier 1 cannot decide -> ROLLBACK, re-prepare (bounded),
                                   then fail closed to CONTEST
   3. policy engine resolves                       quorum/policy/engine.py
   4. execute:
        ACCEPT     insert active
        SUPERSEDE  insert active; close old (valid_to, superseded_by)  [I4]
        REINFORCE  bump evidence_count; do NOT insert
        REJECT     insert with status='rejected' (kept for audit)
        CONTEST    insert contested; mark existing contested           [I5]
   5. insert memory_conflict rows for EVERY detection, benign included
  COMMIT
```

The subtle part, and the part a judge will probe: **the probe is advisory, the
in-transaction read is authoritative.** Under SERIALIZABLE, if a concurrent
writer changed the neighbourhood between the two, either the in-transaction read
sees it and we handle it, or the transaction fails with 40001 and `run_txn`
retries into a world where it does. There is no window in which two
contradictory facts both slip through.

## Module map

| path | responsibility |
|---|---|
| `quorum/db/txn.py` | `run_txn()` — the only commit path. Explicit SERIALIZABLE, bounded backoff, counts and returns retries |
| `quorum/db/pool.py` | pool, statement timeouts, conninfo merging, CA bundle resolution |
| `quorum/db/metrics.py` | retries, latency percentiles, embed/adjudicator counts, cost estimate |
| `quorum/memory/keys.py` | subject_key normalization + alias map. The highest-leverage file |
| `quorum/memory/base.py` | `MemoryClient` ABC, shared SQL, recall, both act() variants |
| `quorum/memory/naive.py` | honest baseline: autocommit, dedup, no policy, no gate |
| `quorum/memory/txn_only.py` | serializable, plain INSERT, no semantics |
| `quorum/memory/quorum.py` | the full two-phase path |
| `quorum/memory/factory.py` | **the only place that branches on mode** |
| `quorum/detect/coerce.py` | value coercion — dates, money, ranges |
| `quorum/detect/tier1.py` | structural classifier. Pure, deterministic, no I/O |
| `quorum/detect/tier2.py` | bounded LLM adjudicator, fail-closed |
| `quorum/policy/rules.py` | R1–R4 as pure functions |
| `quorum/policy/engine.py` | ordered evaluation, multi-neighbour severity |
| `quorum/harness/driver.py` | one workload driver, mode-parameterised |
| `quorum/harness/anomaly.py` | the five post-run detectors |
| `quorum/api/server.py` | read-only FastAPI, including `AS OF SYSTEM TIME` |

## Data model

`memory_atom` is the unit of memory: one immutable claim with attribution
(`writer_agent_id`, `writer_role`, `confidence`), a validity interval
(`valid_from` / `valid_to`), a forward pointer (`superseded_by`) and a status
(`active` / `superseded` / `contested` / `rejected`).

Nothing is ever updated in place except `valid_to`, `superseded_by`, `status`,
`evidence_count` and `confidence` — and the `agent_writer` grant is scoped to
exactly those five columns, so append-only is a database guarantee rather than a
convention.

`memory_conflict` records every detection with the detector tier, similarity,
verdict, resolution, which policy rule fired and why. `action_log` records every
action with its `required_keys`, gate result and `justifying_atom_ids` — the link
that lets you say "this booking was made because of exactly these atoms, and
here is the one that was wrong."
