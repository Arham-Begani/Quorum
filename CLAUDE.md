# CLAUDE.md — Quorum

> Context file for Claude Code / Cursor working in this repository.
> Read this in full before writing any code. The invariants in §3 are not style
> preferences; violating them makes the project pointless.

---

## 1. What this project is

**Quorum is a memory consistency layer for multi-agent systems, built on CockroachDB.**

One-line thesis:

> Transactions solve *write* conflicts. They do not solve *semantic* conflicts.
> Two agents can write mutually contradictory facts as two different rows, both
> commit cleanly under SERIALIZABLE, and the swarm now holds memory that is
> internally inconsistent and will produce a wrong action. Quorum detects
> semantic contradiction **inside the transaction that commits the write**,
> resolves it under an explicit policy, and refuses to let a downstream action
> execute against contested memory.

Why CockroachDB is load-bearing and not decorative — memorize this argument, it is
the spine of the entire submission:

1. Semantic contradiction detection is a **read-modify-write**: read the semantic
   neighbourhood, decide, then write.
2. A read-modify-write is only sound under **serializable isolation**. Without it,
   two agents concurrently writing contradictory facts each read a neighbourhood
   that does not yet contain the other, each conclude "no conflict," and both
   commit. The contradiction checker silently passes and the corruption is worse
   than having no checker, because now you *trust* the memory.
3. The neighbourhood read is a **vector ANN search**. So the vector index and the
   transactional rows must live in the same transactional domain. If embeddings
   are in Pinecone and rows are in Postgres, this operation **cannot be made
   atomic at all** — there is no transaction that spans both systems.
4. CockroachDB is the only system in the required tool list that gives you
   distributed serializable transactions over vectors and rows simultaneously.

That is the whole project. Everything below serves it.

**Demo domain:** *Atlas Travel* — a concierge swarm planning one trip with four
parallel specialist agents (flights, lodging, ground transport, budget/policy).
The domain exists to make a memory inconsistency produce a **visibly wrong
action** on camera: a hotel booked for the wrong night, a car booked for a day
nobody is in the city, a booking that violates a budget ceiling another agent
already recorded.

**Hackathon:** CockroachDB × AWS — Build with Agentic Memory. Deadline
2026-08-18 17:00 EDT. Five equally weighted criteria: Agentic Memory Design,
Technological Implementation, Real-World Impact, Product Readiness, Creativity &
Originality.

---

## 2. The three-mode rule (most important design decision in the repo)

Every workload runs in one of three modes. **Never delete a mode. Never let the
modes diverge in workload, seed, or agent logic.** The only difference between
them is the memory layer.

| Mode | Storage | Txn | Semantic layer | Expected outcome |
|---|---|---|---|---|
| `naive` | separate vector store + separate row store | none | none | lost updates, dirty reads, contradictory memory, **wrong booking** |
| `txn_only` | CockroachDB, SERIALIZABLE | yes | **none** | zero lost updates, zero dirty reads — **but still contradictory memory and still a wrong booking** |
| `quorum` | CockroachDB, SERIALIZABLE | yes | full | contradiction detected, resolved or contested, **wrong booking blocked** |

`txn_only` is the single most important column in this project.

A Cockroach Labs judge's first objection will be *"isn't this just the database
working as designed?"* The `txn_only` mode is the answer: it is CockroachDB, used
correctly, with serializable isolation and no anomalies — **and it still produces
the wrong booking**, because the contradiction lives across two structurally
unrelated rows that no isolation level will ever flag. That is the gap Quorum
fills, and it is what makes this engineering rather than configuration.

If a change would make `txn_only` and `quorum` produce the same outcome on the
canonical scenarios, the change is wrong. Stop and re-read this section.

---

## 3. Invariants — never violate these

**I1. No network calls inside an open transaction.**
Embeddings (Bedrock Titan) and LLM adjudication (Bedrock Claude) are network
calls with unbounded tail latency. Holding a serializable transaction open across
them will produce contention collapse and cascading 40001 retries under the swarm
workload. All embedding and adjudication happens **before** `BEGIN`. See §6 for
the prepared-decision pattern that makes this sound.

**I2. The neighbourhood read and the write are in the same transaction.**
The ANN search that finds conflict candidates and the INSERT/supersede that
follows must be one transaction. This is the entire reason CockroachDB is here.
Splitting them is the bug this project exists to demonstrate.

**I3. Every write path is retry-wrapped.**
CockroachDB returns `40001 serialization_failure` under contention by design.
Every transaction goes through `quorum.db.run_txn()` which implements bounded
exponential backoff with jitter. Never call `conn.commit()` outside that helper.
Retries are **counted and surfaced** — they are demo material, not noise.

**I4. Memory is append-only. Nothing is ever UPDATEd in place except `valid_to`,
`superseded_by`, `status`, `evidence_count`, and `confidence`.**
Supersession sets `valid_to = now()` and `superseded_by = <new_id>` on the old
atom and inserts a new one. This preserves the historical record for the
`AS OF SYSTEM TIME` forensic view and for the audit story. There is no `DELETE`
in the memory write path. Ever.

**I5. Contested memory never silently resolves.**
When the policy engine cannot resolve a contradiction, both atoms are marked
`contested` and **the action gate blocks any action depending on that subject
key**. Refusing to act is a feature and a demo beat. Do not add a fallback that
picks one arbitrarily to keep the demo moving.

**I6. Every memory atom carries writer attribution.**
`writer_agent_id`, `writer_role`, and `confidence` are NOT NULL. An unattributed
memory is unresolvable by the policy engine and undebuggable by a human.

**I7. Reads are scoped.**
Every `recall()` filters by `workspace_id` and by the calling agent's visibility
scope. A sub-agent does not inherit its parent's full memory. Cross-workspace
leakage is a demo-ending bug and a Product Readiness score killer.

**I8. The three modes share one workload driver, one seed, one agent
implementation.**
The mode is injected as a memory-client implementation, nothing else. If you find
yourself writing `if mode == "naive"` anywhere outside `quorum/memory/factory.py`,
you have broken the comparison and the demo is no longer honest.

**I9. Determinism where it matters.**
The canonical scenarios must reproduce their contradictions on every run. Seed
all RNG. Pin agent temperature to 0 for scenario runs. A demo that only sometimes
shows the bug is not a demo.

**I10. Cost and latency are observable.**
Every embedding call, adjudication call, and transaction is counted and timed.
Product Readiness is a scored criterion; "we never measured it" scores zero.

---

## 4. Architecture

```
                        ┌───────────────────────────────┐
                        │   Atlas Travel Swarm (Lambda) │
                        │  flights · lodging · ground   │
                        │       · budget/policy         │
                        └───────────────┬───────────────┘
                                        │ remember() / recall() / act()
                        ┌───────────────▼───────────────┐
                        │      Quorum Memory Client      │
                        │  (naive | txn_only | quorum)   │
                        └───────────────┬───────────────┘
                                        │
             ┌──────────────────────────┼──────────────────────────┐
             │                          │                          │
   ┌─────────▼─────────┐   ┌────────────▼───────────┐  ┌───────────▼──────────┐
   │  Prepare phase    │   │   Commit phase (TXN)   │  │    Action gate       │
   │  (outside txn)    │   │                        │  │                      │
   │ · embed claim     │   │ · ANN neighbourhood    │  │ · require subject    │
   │ · tier-1 classify │   │ · tier-2 verdict apply │  │   keys uncontested   │
   │ · tier-2 adjudicate│  │ · policy resolve       │  │ · else BLOCK         │
   │   (bounded)       │   │ · insert + supersede   │  │ · emit action_log    │
   │ · build decision  │   │ · log conflict         │  │                      │
   └───────────────────┘   └────────────┬───────────┘  └──────────────────────┘
                                        │
                        ┌───────────────▼───────────────┐
                        │        CockroachDB Cloud       │
                        │  memory_atom (+ VECTOR index)  │
                        │  memory_conflict · action_log  │
                        │  agent_registry · provenance   │
                        └───────────────┬───────────────┘
                                        │
             ┌──────────────────────────┼──────────────────────────┐
   ┌─────────▼─────────┐   ┌────────────▼───────────┐  ┌───────────▼──────────┐
   │ Managed MCP Server│   │   Memory Health UI     │  │     ccloud CLI       │
   │ read-only auditor │   │  3-mode split screen   │  │ provisioning · RBAC  │
   │ via Claude Code   │   │  conflicts · retries   │  │ service accts · audit│
   └───────────────────┘   └────────────────────────┘  └──────────────────────┘

AWS: Bedrock (Titan embeddings + Claude adjudicator/agents) · Lambda (swarm
fan-out) · S3 (run traces, artifacts) · CloudWatch (metrics)
```

### Required-tool accounting

CockroachDB tools used (need ≥2, we use 3, optionally 4):

1. **Distributed Vector Indexing** — the conflict-candidate neighbourhood search.
   Load-bearing: without ANN over embeddings there is no semantic contradiction
   detection.
2. **Cloud Managed MCP Server** — read-only auditor persona. A human opens Claude
   Code, connects to the cluster, and interrogates memory state and the conflict
   log during the demo. Load-bearing for the Product Readiness story
   (observability + audit logging + read-only safety).
3. **ccloud CLI** — cluster provisioning, per-agent-role service accounts with
   RBAC, audit log retrieval into the dashboard. Load-bearing for access control.
4. *(optional, if time)* **Agent Skills repo** — consume their schema-design and
   performance skills to review the Quorum schema; document what they caught.

AWS services used (need ≥1, we use 4): Bedrock, Lambda, S3, CloudWatch.

---

## 5. Data model

Authoritative DDL lives in `sql/001_schema.sql`. This section explains *semantics*
— what each field means and why it exists. Do not change the schema without
updating both.

### `memory_atom` — the unit of memory

| Column | Type | Meaning |
|---|---|---|
| `id` | UUID PK | atom identity |
| `workspace_id` | UUID | tenant/trip scope. Every read filters on this. |
| `subject_key` | STRING | **normalized** entity+attribute key, e.g. `trip:42:hotel.checkin_date`. The cheap structural handle for conflict detection. |
| `predicate` | STRING | relation name, e.g. `equals`, `prefers`, `forbids` |
| `object_text` | STRING | the claim in natural language, as written by the agent |
| `object_json` | JSONB | structured value when parseable (`{"date":"2026-09-14"}`). Enables tier-1 deterministic comparison. NULL when unparseable. |
| `embedding` | VECTOR(1024) | Titan v2 embedding of `subject_key + predicate + object_text` |
| `writer_agent_id` | STRING | which agent instance wrote it |
| `writer_role` | STRING | FK → `agent_registry.role`. Drives authority tier. |
| `confidence` | FLOAT | agent's self-reported confidence [0,1] |
| `evidence_count` | INT | corroborations. Incremented by REINFORCE. |
| `valid_from` | TIMESTAMPTZ | when this became true |
| `valid_to` | TIMESTAMPTZ NULL | NULL ⇒ **currently valid**. Set on supersession. |
| `superseded_by` | UUID NULL | forward pointer to the atom that replaced it |
| `status` | STRING | `active` \| `superseded` \| `contested` \| `rejected` |
| `visibility` | STRING | `workspace` \| `role` \| `private` |
| `created_at` | TIMESTAMPTZ | wall clock |

Indexes:
- vector index on `embedding` (the C-SPANN distributed vector index)
- `(workspace_id, subject_key) WHERE valid_to IS NULL` — the hot structural lookup
- `(workspace_id, status) WHERE valid_to IS NULL`

**`subject_key` normalization is critical.** Two agents writing about the same
attribute must produce the same key or tier-1 detection misses and you fall
through to expensive, fuzzier tier-2. Normalization rules live in
`quorum/memory/keys.py` and are unit-tested. Treat that file as high-value.

### `memory_conflict` — the demo's gold

Every detection, resolved or not: `id`, `workspace_id`, `incoming_atom_id`,
`existing_atom_id`, `subject_key`, `detector` (`tier1_structural` |
`tier2_semantic`), `similarity`, `verdict` (`agreement` | `refinement` |
`contradiction` | `unrelated`), `resolution` (`accept` | `supersede` |
`reinforce` | `reject` | `contest`), `policy_rule` (which rule fired),
`rationale`, `detected_at`.

This table is what the dashboard renders and what the MCP auditor queries. Write
to it on **every** detection including benign ones — the ratio of benign to
contradictory detections is itself a credibility signal.

### `action_log` — where memory becomes consequence

`id`, `workspace_id`, `agent_id`, `action_type`, `payload`, `required_keys[]`,
`gate_result` (`allowed` | `blocked_contested` | `blocked_missing` |
`blocked_ambiguous`), `justifying_atom_ids[]`, `executed`, `outcome`,
`created_at`.

`justifying_atom_ids` is the link that lets you say, on camera, *"this booking was
made because of exactly these three memory atoms, and here is the one that was
wrong."*

### `agent_registry`

`agent_id`, `role`, `authority_tier` (INT, lower = more authoritative),
`visibility_scopes[]`, `created_at`.

Authority tiers for Atlas Travel:

| Tier | Roles | Rationale |
|---|---|---|
| 1 | `booking_agent`, `confirmation_agent` | reports *confirmed external facts* — a booking reference is ground truth |
| 2 | `policy_agent`, `budget_agent` | reports *constraints* — authoritative over preferences |
| 3 | `flight_agent`, `lodging_agent`, `ground_agent` | reports *plans and proposals* |
| 4 | `research_agent` | reports *inferences* — lowest authority |

### `memory_provenance`

`derived_atom_id`, `source_atom_id`, `relation`. Edges for derived/summarized
memory. Enables the "delete the root and the inference dies too" beat if you add
erasure as a stretch.

---

## 6. Core algorithms

### 6.1 Write path — `remember(claim)`

Two phases. This split exists to satisfy I1 (no network in txn) without breaking
I2 (neighbourhood read and write in one txn).

**Phase A — prepare (outside transaction, no locks held):**

```
1. normalize subject_key
2. parse object_json if possible
3. embedding = bedrock.embed(subject_key + predicate + object_text)   [network]
4. probe_neighbours = SELECT ... ORDER BY embedding <=> $e LIMIT k     [read-only,
   own txn, may be stale — this is a PROBE, not the decision]
5. for each probe neighbour:
      tier-1 structural classify (deterministic, no network)
      if inconclusive AND similarity > τ_adjudicate:
          tier-2 LLM adjudicate  [network, bounded: max ADJUDICATE_BUDGET calls]
6. build PreparedDecision {
      embedding, verdicts: {existing_atom_id -> verdict},
      probe_neighbour_ids, probe_read_ts
   }
```

**Phase B — commit (inside one serializable transaction):**

```
BEGIN;
1. authoritative_neighbours = ANN search again, INSIDE the txn
2. new_ids = authoritative_neighbours - probe_neighbour_ids
   if new_ids is non-empty:
        -- a concurrent writer landed between probe and commit
        if any new_id is structurally conflicting (tier-1, deterministic, no network):
              apply verdict now
        else:
              ROLLBACK and re-run Phase A with the new neighbourhood
              (bounded to REPREPARE_MAX attempts, then fail closed to CONTEST)
3. resolve via policy engine using verdicts
4. execute resolution:
     ACCEPT     -> INSERT new atom (status=active)
     SUPERSEDE  -> INSERT new atom; UPDATE old SET valid_to=now(),
                   superseded_by=new.id, status='superseded'
     REINFORCE  -> UPDATE existing SET evidence_count+=1,
                   confidence=max(...); do NOT insert
     REJECT     -> INSERT new atom with status='rejected' (kept for audit)
     CONTEST    -> INSERT new atom status='contested';
                   UPDATE existing SET status='contested'
5. INSERT memory_conflict rows for every detection
COMMIT;
```

Step 2 is the subtle part and the part a judge will probe. Say it plainly in the
README and the video: **the probe is advisory, the in-transaction read is
authoritative.** Under SERIALIZABLE, if a concurrent writer changed the
neighbourhood, either our in-txn read sees it (and we handle it) or the
transaction fails with 40001 and retries. There is no window where two
contradictory facts both slip through. In `naive` mode that window is wide open,
and the harness proves it.

Tier-1 handling inside the transaction is deliberately restricted to
deterministic structural comparison — no network, satisfying I1. The expensive
tier-2 path only ever runs in Phase A.

### 6.2 Conflict classification

**Tier 1 — structural (deterministic, ~0ms, no cost).** Fires when
`subject_key` matches and both atoms have comparable `object_json`.

- same key, equal scalar values → `agreement`
- same key, different scalar values, both `equals` predicate → `contradiction`
- same key, one value is a strict refinement of the other (range → point,
  null → value) → `refinement`
- `forbids` vs `equals` on the same key with an overlapping value →
  `contradiction`
- otherwise → inconclusive, fall through to tier 2

Tier 1 should catch the majority of the canonical scenarios. Design the scenarios
so it does — it makes the system fast, cheap, and demonstrably not
"just ask an LLM."

**Tier 2 — semantic (LLM adjudicator, bounded).** Only for pairs above
`τ_adjudicate` cosine similarity that tier 1 could not decide. The adjudicator
returns strict JSON `{verdict, confidence, rationale}` with verdict in
{agreement, refinement, contradiction, unrelated}. Temperature 0. On timeout,
parse failure, or throttle: **fail closed to `contradiction` → CONTEST**. Never
fail open. A false contest is a visible, safe outcome; a missed contradiction is
the exact failure this project exists to prevent.

Budget: `ADJUDICATE_BUDGET` calls per `remember()` (default 3), and a global
per-run ceiling. Log every call with latency and token count.

### 6.3 Resolution policy engine

Ordered rules, first match wins. Each returns `(resolution, policy_rule,
rationale)`. Implemented as a list of pure functions in
`quorum/policy/rules.py` so they are trivially unit-testable and easy to explain
on a slide.

```
R1 AUTHORITY   if tier(incoming) < tier(existing):  SUPERSEDE
               if tier(incoming) > tier(existing):  REJECT
               else fall through
R2 EVIDENCE    if |evidence_incoming - evidence_existing| >= EVIDENCE_MARGIN:
                    higher wins  -> SUPERSEDE or REJECT
R3 RECENCY     if same tier and both low-evidence and
                  confidence_incoming >= confidence_existing - CONF_EPSILON:
                    SUPERSEDE (newer wins within a tier)
R4 CONTEST     otherwise: CONTEST both
```

`agreement` verdicts short-circuit to REINFORCE. `refinement` short-circuits to
SUPERSEDE with `policy_rule = 'refinement'`. `unrelated` short-circuits to
ACCEPT.

R4 is not a failure mode, it is the safety net. Tune the thresholds so at least
one canonical scenario lands in R4 — a demo where the system *declines to guess*
and escalates to a human is more convincing than one where it always has an
answer.

### 6.4 Read path — `recall(query, agent, workspace, as_of=None)`

```
1. hybrid retrieval:
     a. exact: WHERE workspace_id=$w AND subject_key = ANY($keys)
               AND valid_to IS NULL
     b. semantic: ORDER BY embedding <=> embed(query) LIMIT k
                  WHERE workspace_id=$w AND valid_to IS NULL
2. visibility filter by agent role scopes            [I7]
3. contested atoms are RETURNED but FLAGGED, never dropped silently  [I5]
4. attach attribution (writer_role, confidence, evidence_count) to every result
5. if as_of is set: run the whole query AS OF SYSTEM TIME $as_of
```

The `as_of` path powers the forensic view: *"what did the swarm believe at
14:32:07, right before it made the booking?"* This is cheap to build on top of
the append-only model and buys a disproportionate amount of Creativity score.

**GC TTL warning:** `AS OF SYSTEM TIME` can only read within the garbage
collection window. Set `gc.ttlseconds` high on the memory tables at provisioning
time (see BUILD.md §3). The default is far too short for a demo recorded days
after a run. This has killed this feature for other people; do it on day one.

### 6.5 Action gate — `act(action, required_keys)`

```
for each key in required_keys:
    atoms = current valid atoms for (workspace, key)
    if len(atoms) == 0                     -> BLOCK (missing)
    if any atom.status == 'contested'      -> BLOCK (contested)
    if len(active atoms) > 1               -> BLOCK (ambiguous)
if all clear:
    execute action, log justifying_atom_ids
```

In `naive` and `txn_only` modes the gate still runs — but it passes, because
nothing was ever marked contested. That is precisely how the wrong booking gets
through, and it is the moment the video is built around.

---

## 7. Repository layout

```
quorum/
  db/
    pool.py            connection pool, statement timeouts
    txn.py             run_txn() retry wrapper — THE ONLY commit path  [I3]
    metrics.py         retry counters, latency histograms
  memory/
    base.py            MemoryClient ABC: remember/recall/act
    naive.py           mode: naive        (separate stores, no txn, no semantics)
    txn_only.py        mode: txn_only     (CRDB txn, no semantic layer)
    quorum.py          mode: quorum       (full)
    factory.py         THE ONLY place that branches on mode        [I8]
    keys.py            subject_key normalization (high-value, well-tested)
    schema.py          row <-> dataclass mapping
  detect/
    tier1.py           structural classifier (pure, deterministic)
    tier2.py           LLM adjudicator (Bedrock, bounded, fail-closed)
    prompts.py         adjudicator prompt (versioned, do not edit casually)
  policy/
    rules.py           R1..R4 as pure functions
    engine.py          ordered evaluation
    tiers.py           role -> authority tier
  embed/
    bedrock.py         Titan v2 client, batching, cache
    cache.py           content-hash embedding cache (cost control)
  agents/
    base.py            LangChain agent scaffold + Bedrock binding
    flights.py lodging.py ground.py budget.py research.py
    tools.py           booking simulator tools (all go through act())
  domain/
    inventory.py       mock flight/hotel/car inventory (seeded, deterministic)
    scenarios/         canonical contradiction scenarios (see §8)
  harness/
    driver.py          ONE workload driver, mode-parameterized       [I8]
    anomaly.py         lost-update / dirty-read / contradiction detectors
    report.py          run report -> JSON -> S3
  mcp/
    README.md          how to attach Claude Code to the cluster read-only
    queries.md         curated auditor queries for the demo
  api/
    server.py          FastAPI: runs, conflicts, atoms, timeline, as_of
dashboard/             Next.js 3-mode split screen + memory health
sql/
  001_schema.sql  002_indexes.sql  003_zone_configs.sql  004_seed_registry.sql
infra/
  ccloud/            provisioning + service account scripts
  lambda/            handler + packaging
  cloudformation/    or CDK — swarm fan-out, S3, IAM
tests/
  unit/  integration/  scenarios/  chaos/
docs/
  ARCHITECTURE.md  CONSISTENCY_MODEL.md  DEMO_SCRIPT.md  SUBMISSION.md
BUILD.md
CLAUDE.md
LICENSE            <- Apache-2.0. Required by hackathon rules. Do not omit.
README.md
```

---

## 8. Canonical scenarios

These are the scripted contradictions. They must reproduce **deterministically**
(I9) and each must produce a different, legible failure in `naive`/`txn_only`.
Live in `quorum/domain/scenarios/`.

| ID | Contradiction | Tier | Expected `quorum` resolution | Wrong action if unguarded |
|---|---|---|---|---|
| `S1_checkin_date` | lodging agent writes check-in Sep 14; booking agent's confirmed itinerary implies arrival Sep 15 | tier 1 | SUPERSEDE via R1 (booking_agent tier 1 > lodging tier 3) | hotel booked for the wrong night, guest arrives to no room |
| `S2_budget_ceiling` | budget agent writes ceiling $2,400; research agent infers "traveller is flexible on price" | tier 2 | REJECT via R1 (policy tier 2 beats research tier 4) | booking exceeds policy, expense rejected |
| `S3_ground_overlap` | two ground agents each book airport transfer for the same slot | tier 1 | CONTEST via R4 (same tier, same evidence, same confidence) | double-booked and double-charged transfer |
| `S4_preference_reversal` | traveller earlier "prefers email updates"; later "stop emailing me" | tier 2 | SUPERSEDE via R3 (recency within tier) | agent keeps emailing after opt-out — a compliance-flavoured failure |
| `S5_concurrent_race` | two agents write contradictory check-in dates **simultaneously** | tier 1 | one commits, other hits 40001, retries, sees the first, CONTESTs | **both commit in `naive`; memory holds two truths** |

`S5` is the flagship. It is the only scenario that isolates the isolation-level
argument, and it is the one that makes `naive` fail in a way `txn_only` does not.
Build `S5` first. If you cannot reproduce a double-commit in `naive` mode on
demand, the central claim is unproven and you must know that in week one.

---

## 9. Concurrency, performance, and cost rules

- **Transaction budget:** target < 25ms of work inside any transaction. If a
  transaction exceeds `TXN_SLOW_MS` (default 100), log a warning with the
  statement breakdown. Long transactions under a swarm workload cause retry
  storms that will make your own system look bad on camera.
- **Retry handling:** `run_txn()` retries on 40001 with exponential backoff +
  jitter, max `TXN_MAX_RETRIES` (default 8). Beyond that, fail the write and log
  it — do not silently drop. Retry counts are exported to the dashboard because a
  *visible, bounded* retry count is a Product Readiness signal, not an
  embarrassment.
- **ANN tuning:** vector search `LIMIT k` default 8. Higher k finds more
  conflicts and costs more latency. `k` is a config knob and the tradeoff belongs
  in `docs/CONSISTENCY_MODEL.md` — judges reward a stated, measured tradeoff.
- **Embedding cache:** content-hash keyed. Agents re-assert the same claims
  constantly; caching cuts both cost and latency substantially. Report cache hit
  rate.
- **Bedrock throttling:** expect it under swarm fan-out. Exponential backoff on
  `ThrottlingException`, and a global concurrency semaphore. A throttle must
  never silently become a missed contradiction — it goes through the fail-closed
  path (§6.2).
- **Cost ceiling:** track spend per run in the report. Free-tier CockroachDB is
  the target; if the workload needs more, that is a finding worth documenting,
  not hiding.

---

## 10. Security and access control

Product Readiness is a scored criterion and most entrants will ignore this
entirely. Cheap points:

- **Per-role service accounts** provisioned via ccloud CLI. `research_agent` gets
  read + insert on `memory_atom`; only the gate service can write `action_log`;
  the MCP auditor account is **read-only**.
- **Managed MCP Server in read-only mode** with audit logging on — this is the
  documented default and it is a talking point, not a limitation.
- **Row-level workspace scoping** enforced in every query, tested with a negative
  test that asserts cross-workspace reads return zero rows.
- **No secrets in the repo.** `.env.example` only. The submission repo is public;
  a leaked connection string is a disqualifying-grade embarrassment.
- **Least privilege on Lambda IAM** — Bedrock invoke + S3 put on one prefix.
  Nothing else.

---

## 11. Testing

Non-negotiable tests:

- `tests/unit/test_keys.py` — subject_key normalization, including adversarial
  spacing/casing/aliasing. High value: bad normalization silently degrades
  detection.
- `tests/unit/test_tier1.py` — every structural classification branch.
- `tests/unit/test_policy.py` — R1..R4 in isolation, plus rule-ordering tests.
- `tests/integration/test_txn_isolation.py` — **the flagship test.** Two threads
  write contradictory atoms concurrently; assert exactly one active atom or a
  contested pair, never two active contradictory atoms. Run it 100× in CI. This
  test *is* the thesis, expressed as code. Point at it in the README.
- `tests/scenarios/test_S1..S5.py` — each canonical scenario across all three
  modes, asserting the expected divergence. If `txn_only` ever matches `quorum`
  on S1–S5, the test fails loudly (see §2).
- `tests/chaos/test_node_kill.py` — writes continue and no anomaly leaks while a
  node is down.

Run scenario tests with temperature 0 and fixed seeds (I9).

---

## 12. Anti-goals — do NOT build these

- **A general-purpose memory SDK with pluggable backends.** Scope trap. One
  backend, three modes, done.
- **LLM-based semantic merge of contradictory facts.** Tempting and impressive
  sounding; it is unreliable, unexplainable, and it destroys the CONTEST beat
  which is your best safety story. Rule-based resolution with an LLM only as a
  *classifier* is the defensible design.
- **A knowledge graph.** Adjacent, interesting, and a different project. The
  subject_key/predicate/object triple is enough structure.
- **Real booking APIs.** Mock inventory. Zero value, high risk, possible ToS
  problems.
- **Authentication, user accounts, multi-tenancy UI.** One workspace, seeded.
- **Fine-tuning anything.**
- **A chat UI.** The dashboard is an *observability* surface, not a chatbot. A
  chat box invites judges to freestyle-test your agent instead of watching your
  consistency model.
- **Streaming/token-by-token UI polish.** Zero rubric points.
- **Rewriting the retry logic per-module.** One `run_txn()`. (I3)

---

## 13. Submission constraints that bind engineering

These are hackathon rules, and they constrain the code:

- **Public repo, OSS licence detectable at the top of the GitHub About section.**
  Apache-2.0. Add it at repo creation, not at the end.
- **Functional demo URL** must be live and work without credentials, or with
  credentials included in the submission. Keep a seeded read-only demo workspace
  that always renders a completed 3-mode comparison, so a judge who never runs a
  workload still sees the whole story.
- **Video under 3 minutes and it must show the memory layer at work.** Not the
  app — the memory. Screen time budget: ~25s wrong booking in `naive`, ~30s
  `txn_only` still wrong (the pivot), ~45s `quorum` catching and contesting,
  ~30s MCP auditor + forensic `as_of` view, ~20s node kill, ~20s architecture.
- **README must let a judge run it cold.** Setup, env vars, seed command, one
  command to reproduce S5. Judges are not required to run anything — make it
  trivial anyway, because the ones who do will score you higher.
- **Explicitly enumerate which CockroachDB tools and AWS services you used and
  what the agent actually did with them.** There is a submission field for this.
  Draft it in `docs/SUBMISSION.md` as you build, not the night before.
- **Architecture diagram** is optional in the rules. Include it. It is free
  signal on Technological Implementation.

---

## 14. Glossary

- **Memory atom** — one immutable claim with attribution and validity interval.
- **Subject key** — normalized `entity:id:attribute` handle; the structural
  identity of a claim.
- **Neighbourhood** — the top-k ANN result set around an incoming claim; the
  candidate set for conflict.
- **Probe read** — advisory, pre-transaction neighbourhood read (Phase A).
- **Authoritative read** — in-transaction neighbourhood read (Phase B); the one
  that counts.
- **Verdict** — the classifier's judgement of a *pair*: agreement / refinement /
  contradiction / unrelated.
- **Resolution** — the policy engine's decision about the *write*: accept /
  supersede / reinforce / reject / contest.
- **Contested** — two atoms the policy could not adjudicate; blocks dependent
  actions.
- **Supersession** — replacing a valid atom while preserving it for history.
- **Action gate** — the check that converts memory consistency into a safety
  property.

---

## 15. Known gotchas

1. **GC TTL kills `AS OF SYSTEM TIME`.** Raise `gc.ttlseconds` at provisioning.
   Verify by reading a 24-hour-old timestamp before building any UI on it.
2. **Vector dimension mismatch is a hard error.** Titan v2 supports 1024/512/256.
   Pin it in config; changing it means re-embedding everything.
3. **Vector index syntax and availability vary by CockroachDB version.** Verify
   `CREATE VECTOR INDEX` against your actual cluster version before building on
   it. If the distributed vector index is unavailable on your tier, fall back to
   brute-force cosine over a bounded candidate set — the consistency argument is
   unaffected, and say so honestly in the README.
4. **40001 is normal.** It is the system working. Surface it, bound it, do not
   hide it, do not treat it as a bug.
5. **Bedrock model IDs and regional availability change.** Keep them in env vars;
   never hardcode. Confirm what is enabled in your region before writing agent
   code.
6. **`naive` mode must be a *fair* baseline.** It should be what a competent
   engineer would actually build with a separate vector store — not a strawman
   with deliberate bugs. If the baseline is unfair, the whole comparison is
   worthless and a sharp judge will catch it. Document exactly what `naive` does
   in `docs/CONSISTENCY_MODEL.md`.
7. **Do not let the LLM adjudicator become the story.** It is a bounded
   classifier of last resort. Tier 1 should carry most of the load. If tier 2
   fires on everything, your subject_key normalization is broken.
