# BUILD.md — Quorum

Implementation guide. Organized by **component**, in dependency order — not by
calendar. Build top to bottom; each section states what "done" means so you can
gate on it.

Read `CLAUDE.md` first. This document assumes its vocabulary and invariants.

> **Build order that matters:** M0 → M1 → **M2 (proof spike)** → M3 → M4 → M5 → M6 → M7 → M8 → M9.
> M2 is a go/no-go gate. Do not build agents, UI, or Lambda before M2 passes.
> If M2 fails, the project's central claim is unproven and you should pivot to
> the fallback in §14 while there is still time.

---

## 0. Prerequisites

### Accounts
- **CockroachDB Cloud** — free tier, no card. `cockroachlabs.cloud/signup`
- **AWS** — free tier. Bedrock model access must be **explicitly requested and
  granted** per model in your region; do this on day one, it is not instant.
- **GitHub** — public repo, Apache-2.0 licence added at creation.

### Local toolchain
```bash
python 3.11+
node 20+                # dashboard only
ccloud                  # CockroachDB Cloud CLI
aws  cli v2
cockroach sql           # optional, ccloud can shell you in
docker                  # optional, for local 3-node chaos testing
```

### Python dependencies
```
psycopg[binary,pool]>=3.1     # raw driver — explicit transaction control
langchain>=0.3
langchain-aws                 # Bedrock chat + embeddings
langchain-community
boto3
fastapi + uvicorn
pydantic>=2
tenacity                      # backoff (or hand-roll; see §4.2)
pytest, pytest-asyncio, pytest-repeat
structlog
```

> **Why raw psycopg for the memory core:** transaction boundaries are the entire
> thesis. A framework that manages sessions for you will hide exactly the thing
> you need to demonstrate. Use LangChain for the *agents* and Bedrock bindings;
> use psycopg for everything under `quorum/db/` and `quorum/memory/`.

### Environment (`.env.example` — commit this, never `.env`)
```bash
# CockroachDB
CRDB_URL=postgresql://<user>@<host>:26257/quorum?sslmode=verify-full
CRDB_URL_AUDITOR=postgresql://auditor@...      # read-only role
CRDB_APP_NAME=quorum

# AWS / Bedrock
AWS_REGION=us-east-1
BEDROCK_EMBED_MODEL_ID=amazon.titan-embed-text-v2:0
BEDROCK_EMBED_DIM=1024
BEDROCK_CHAT_MODEL_ID=                # confirm what is enabled in YOUR region
S3_BUCKET=quorum-runs-<suffix>

# Quorum tuning
ANN_K=8
TAU_ADJUDICATE=0.82
ADJUDICATE_BUDGET=3
EVIDENCE_MARGIN=2
CONF_EPSILON=0.05
TXN_MAX_RETRIES=8
TXN_SLOW_MS=100
REPREPARE_MAX=2
RUN_SEED=1337
```

**Never hardcode a Bedrock model ID.** Availability and IDs differ by region and
change over time. Query what is enabled in your account before writing agent code:
```bash
aws bedrock list-foundation-models --region $AWS_REGION \
  --query 'modelSummaries[].modelId' --output table
```

---

## M0 — Repository skeleton and licence

**Do this first, it takes ten minutes and unblocks the submission requirement.**

```bash
mkdir quorum && cd quorum && git init
# Apache-2.0 LICENSE at repo root, chosen via GitHub's licence picker so it is
# detected and shown in the About sidebar. The rules require it be *detectable*.
```

Create the directory tree from `CLAUDE.md` §7 with empty `__init__.py` files.
Add `.gitignore` covering `.env`, `__pycache__`, `.venv`, `node_modules`,
`*.pem`, `runs/`.

**Done when:** GitHub repo page shows "Apache-2.0" in the About section.

---

## M1 — Cluster provisioning via ccloud CLI

This is a required-tool integration, not just setup. **Script it** — a judge
reading `infra/ccloud/provision.sh` sees the CLI used deliberately.

### 1.1 Authenticate and create the cluster
```bash
ccloud auth login
ccloud cluster create serverless quorum-prod \
  --region <aws-region> --cloud AWS
ccloud cluster list --output json      # JSON on every command — agent-friendly
```

### 1.2 Per-role service accounts (this is the RBAC story)

Create one SQL user per authority tier, not one superuser for everything:

```sql
CREATE USER agent_writer;      -- agents: SELECT + INSERT on memory_atom
CREATE USER gate_service;      -- only writer of action_log
CREATE USER auditor;           -- READ ONLY, used by the MCP server
CREATE USER quorum_admin;      -- migrations only

GRANT SELECT, INSERT ON TABLE memory_atom, memory_conflict TO agent_writer;
GRANT UPDATE ON TABLE memory_atom TO agent_writer;   -- deliberately NO DELETE
GRANT SELECT, INSERT ON TABLE action_log TO gate_service;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO auditor;
```

The **absent** `DELETE` grant is worth calling out in the video — the swarm
cannot erase a claim, so half of invariant I4 holds at the database level rather
than by convention. That is the difference between "we thought about security"
and "we implemented it."

> Do not promise a column-scoped `UPDATE` here. CockroachDB has no column-level
> privileges (v26.2.1 rejects the column list outright), and
> `information_schema.column_privileges` expands the table grant across every
> column — so a live query would show `UPDATE` on all 18 and contradict you.
> Restricting writes to the five supersession columns is done in application
> code.

Also create a ccloud service account + API key for the CLI-driven parts:
```bash
ccloud service-account create quorum-agent --description "Quorum control plane"
ccloud api-key create <service-account-id>
```

### 1.3 Audit log retrieval
```bash
ccloud cluster sql-audit-log list --cluster quorum-prod --output json
```
Pipe this into the dashboard's audit panel (M8). Even a read-only view of who
touched memory is a Product Readiness point almost no entrant will have.

**Done when:** `provision.sh` runs end to end from nothing to a cluster with four
roles, and `ccloud cluster list --output json` is captured in the README.

---

## M2 — PROOF SPIKE (go/no-go gate)

**Goal: prove the central claim before building anything else.** No agents, no
LLM, no UI. Two threads, two contradictory writes, three modes.

### 2.1 Minimal schema
Just `memory_atom` with a vector column and the structural index. Skip the rest.

### 2.2 Verify the vector index exists and works
```sql
CREATE TABLE probe (id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    embedding VECTOR(1024));
CREATE VECTOR INDEX ON probe (embedding);
SELECT id FROM probe ORDER BY embedding <=> '[...]'::VECTOR LIMIT 5;
```
If `CREATE VECTOR INDEX` is unavailable on your cluster version or tier, stop and
apply the §14 fallback now — do not discover this in week three.

### 2.3 Set the GC TTL before anything else writes data
```sql
ALTER TABLE memory_atom CONFIGURE ZONE USING gc.ttlseconds = 90000;  -- ~25h
-- verify time travel actually works:
SELECT count(*) FROM memory_atom AS OF SYSTEM TIME '-1h';
```
Re-verify with a 24h-old timestamp the next day. If this fails you lose the
forensic view (§M7) and need to know early.

### 2.4 The spike
Write `spikes/prove_race.py`:

```
two threads, both writing subject_key = "trip:1:hotel.checkin_date"
  thread A: {"date": "2026-09-14"}
  thread B: {"date": "2026-09-15"}
both do: read neighbourhood -> if no conflict, insert

run 200 iterations in each of three modes, count outcomes:
  naive    : expect a high rate of BOTH-COMMITTED (two active contradictory atoms)
  txn_only : expect zero lost updates, but STILL both committed
             (different rows, no constraint violated — this is the point)
  quorum   : expect exactly one active atom, or a contested pair. NEVER two active.
```

Add an artificial delay between the read and the write to widen the race window
so it reproduces reliably. Document that you did this and why — widening a real
window to make it observable is legitimate; inventing one is not.

**GATE — M2 passes only if:**
- [ ] `naive` produces two active contradictory atoms at a measurable rate
- [ ] `txn_only` also produces them (proving isolation alone is insufficient)
- [ ] `quorum` produces zero, across 200+ iterations
- [ ] you observe and count real 40001 retries in `quorum` mode
- [ ] `AS OF SYSTEM TIME '-1h'` returns rows

If any box is unchecked, go to §14 before building further.

---

## M3 — Schema and data layer

### 3.1 `sql/001_schema.sql`

```sql
CREATE DATABASE IF NOT EXISTS quorum;
SET database = quorum;

CREATE TABLE memory_atom (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id    UUID        NOT NULL,
  subject_key     STRING      NOT NULL,
  predicate       STRING      NOT NULL,
  object_text     STRING      NOT NULL,
  object_json     JSONB,
  embedding       VECTOR(1024) NOT NULL,
  writer_agent_id STRING      NOT NULL,
  writer_role     STRING      NOT NULL,
  confidence      FLOAT       NOT NULL DEFAULT 0.5,
  evidence_count  INT         NOT NULL DEFAULT 1,
  valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
  valid_to        TIMESTAMPTZ,
  superseded_by   UUID,
  status          STRING      NOT NULL DEFAULT 'active',
  visibility      STRING      NOT NULL DEFAULT 'workspace',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_status CHECK (status IN
    ('active','superseded','contested','rejected')),
  CONSTRAINT ck_visibility CHECK (visibility IN
    ('workspace','role','private')),
  CONSTRAINT ck_conf CHECK (confidence BETWEEN 0 AND 1)
);

CREATE TABLE memory_conflict (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id      UUID        NOT NULL,
  run_id            UUID        NOT NULL,
  incoming_atom_id  UUID,
  existing_atom_id  UUID        NOT NULL,
  subject_key       STRING      NOT NULL,
  detector          STRING      NOT NULL,   -- tier1_structural | tier2_semantic
  similarity        FLOAT,
  verdict           STRING      NOT NULL,   -- agreement|refinement|contradiction|unrelated
  resolution        STRING      NOT NULL,   -- accept|supersede|reinforce|reject|contest
  policy_rule       STRING,                 -- R1|R2|R3|R4|refinement|agreement
  rationale         STRING,
  adjudicator_ms    INT,
  detected_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE action_log (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id        UUID        NOT NULL,
  run_id              UUID        NOT NULL,
  agent_id            STRING      NOT NULL,
  action_type         STRING      NOT NULL,
  payload             JSONB       NOT NULL,
  required_keys       STRING[]    NOT NULL,
  gate_result         STRING      NOT NULL, -- allowed|blocked_contested|
                                            -- blocked_missing|blocked_ambiguous
  justifying_atom_ids UUID[],
  executed            BOOL        NOT NULL DEFAULT false,
  outcome             STRING,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agent_registry (
  agent_id         STRING PRIMARY KEY,
  role             STRING   NOT NULL,
  authority_tier   INT      NOT NULL,
  visibility_scopes STRING[] NOT NULL DEFAULT ARRAY['workspace'],
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE memory_provenance (
  derived_atom_id UUID NOT NULL,
  source_atom_id  UUID NOT NULL,
  relation        STRING NOT NULL,
  PRIMARY KEY (derived_atom_id, source_atom_id)
);

CREATE TABLE run (
  run_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mode       STRING NOT NULL,          -- naive|txn_only|quorum
  scenario   STRING NOT NULL,
  seed       INT    NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at   TIMESTAMPTZ,
  report     JSONB
);
```

### 3.2 `sql/002_indexes.sql`
```sql
CREATE VECTOR INDEX idx_atom_embedding ON memory_atom (embedding);

CREATE INDEX idx_atom_subject_live ON memory_atom (workspace_id, subject_key)
  WHERE valid_to IS NULL;
CREATE INDEX idx_atom_status_live ON memory_atom (workspace_id, status)
  WHERE valid_to IS NULL;
CREATE INDEX idx_conflict_run ON memory_conflict (run_id, detected_at);
CREATE INDEX idx_action_run ON action_log (run_id, created_at);
```
> Verify `CREATE VECTOR INDEX` syntax against your cluster version. It has
> differed across releases and may require an opclass suffix.

### 3.3 `sql/003_zone_configs.sql`
```sql
ALTER TABLE memory_atom     CONFIGURE ZONE USING gc.ttlseconds = 90000;
ALTER TABLE memory_conflict CONFIGURE ZONE USING gc.ttlseconds = 90000;
ALTER TABLE action_log      CONFIGURE ZONE USING gc.ttlseconds = 90000;
```

### 3.4 `quorum/db/txn.py` — the only commit path (I3)

```python
RETRYABLE = "40001"

def run_txn(pool, fn, *, max_retries=8, label="txn"):
    """Execute fn(cur) inside one serializable transaction. Retries on 40001.
    Records retry count and duration into metrics. THE ONLY place that commits."""
    attempt, backoff = 0, 0.02
    while True:
        t0 = time.perf_counter()
        try:
            with pool.connection() as conn:
                conn.autocommit = False
                with conn.cursor() as cur:
                    result = fn(cur)
                conn.commit()
            metrics.observe_txn(label, time.perf_counter()-t0, attempt)
            return result
        except psycopg.errors.SerializationFailure:
            attempt += 1
            metrics.count_retry(label)
            if attempt > max_retries:
                metrics.count_txn_give_up(label)
                raise
            time.sleep(backoff * (2 ** (attempt-1)) * (0.5 + random.random()))
```

Set a statement timeout on the pool so a pathological transaction cannot wedge a
demo: `options='-c statement_timeout=5000'`.

**Done when:** migrations apply cleanly from empty, and `run_txn` has a unit test
that forces a synthetic 40001 and asserts the retry count.

---

## M4 — Memory core

### 4.1 `quorum/memory/keys.py` — subject key normalization

The highest-leverage file in the repo. If two agents describe the same attribute
with different keys, tier-1 detection misses and you fall through to slow, fuzzy
tier-2.

```
normalize(entity_type, entity_id, attribute) -> "trip:42:hotel.checkin_date"

rules:
  - lowercase everything
  - attribute path segments joined by '.', normalized against an ALIAS_MAP
      {"check_in","checkin","arrival_date","check-in date"} -> "checkin_date"
  - strip whitespace and punctuation from ids
  - unknown attributes pass through normalized but are logged as
    UNMAPPED_ATTRIBUTE — a growing unmapped list means detection is degrading
```

Ship the `ALIAS_MAP` for the Atlas Travel domain. Log unmapped attributes to the
dashboard; being able to say "we measured key-normalization coverage at 94%" is
a strong Technological Implementation signal.

### 4.2 `quorum/embed/bedrock.py`

```python
class Embedder:
    def embed(self, text: str) -> list[float]:
        # 1. content-hash cache lookup
        # 2. bedrock invoke_model, Titan v2, dim from BEDROCK_EMBED_DIM
        # 3. backoff on ThrottlingException (exponential + jitter, max 5)
        # 4. record latency + cost estimate into metrics
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        # batch where the API allows; still cache per-item
```
Cache is content-hash keyed and persisted to disk so repeated scenario runs
during development are near-free. Report hit rate in the run report.

### 4.3 `quorum/memory/base.py` — the interface all three modes implement

```python
class MemoryClient(ABC):
    @abstractmethod
    def remember(self, claim: Claim) -> RememberResult: ...
    @abstractmethod
    def recall(self, query: str, *, agent: AgentCtx,
               subject_keys: list[str] | None = None,
               as_of: datetime | None = None) -> list[Atom]: ...
    @abstractmethod
    def act(self, action: Action) -> GateResult: ...
```

```python
@dataclass(frozen=True)
class Claim:
    workspace_id: UUID; subject_key: str; predicate: str
    object_text: str; object_json: dict | None
    agent_id: str; role: str; confidence: float

@dataclass(frozen=True)
class RememberResult:
    atom_id: UUID | None; resolution: str; policy_rule: str | None
    conflicts: list[ConflictRecord]; retries: int; latency_ms: float
```

### 4.4 `quorum/memory/naive.py` — the honest baseline

Must be what a competent engineer would actually build, not a strawman (see
`CLAUDE.md` §15.6):

- embeddings in an in-process FAISS-style index (or a second table written
  outside any transaction)
- rows in CockroachDB with **autocommit**, one statement at a time
- a conflict check that reads the vector index and then writes — **as two
  separate operations**, which is exactly what everyone builds
- no supersession, no attribution-driven resolution, append-only duplicates

Document this precisely in `docs/CONSISTENCY_MODEL.md`. The fairness of the
baseline is what makes the comparison credible.

### 4.5 `quorum/memory/txn_only.py`

Same storage as `quorum`, same serializable transactions, same retry wrapper —
**but no tier-1/tier-2 classification and no policy engine.** Every write is a
plain INSERT. This mode proves isolation is necessary but not sufficient.

### 4.6 `quorum/memory/quorum.py` — the full path

Implement §6.1 of `CLAUDE.md` exactly: Phase A outside the transaction, Phase B
inside it, with the probe-vs-authoritative neighbourhood reconciliation.

Neighbourhood query (used in both phases):
```sql
SELECT id, subject_key, predicate, object_text, object_json,
       writer_role, confidence, evidence_count, status,
       embedding <=> $1 AS distance
FROM memory_atom
WHERE workspace_id = $2
  AND valid_to IS NULL
  AND status IN ('active','contested')
ORDER BY embedding <=> $1
LIMIT $3;
```
Union this with an exact `subject_key = $4` lookup so a structural match is never
missed because ANN recall dropped it. **Do not rely on ANN alone for tier-1.**

### 4.7 `quorum/memory/factory.py`

```python
def make_memory(mode: str, pool, embedder, cfg) -> MemoryClient:
    return {"naive": NaiveMemory, "txn_only": TxnOnlyMemory,
            "quorum": QuorumMemory}[mode](pool, embedder, cfg)
```
The **only** place in the codebase that branches on mode (I8). Enforce with a
grep-based lint in CI.

**Done when:** all three clients pass the same interface test suite, and
`tests/integration/test_txn_isolation.py` passes 100 consecutive runs.

---

## M5 — Detection and policy

### 5.1 `quorum/detect/tier1.py` — pure, deterministic, fast

```python
def classify(incoming: Claim, existing: Atom) -> Verdict | None:
    """Return a Verdict, or None if inconclusive (escalate to tier 2).
    MUST be pure: no I/O, no clock, no randomness."""
```
Branches to implement (each gets a unit test):
- key mismatch → `None` (tier 2 decides on semantic similarity alone)
- key match, both `object_json` scalars, equal → `AGREEMENT`
- key match, both scalars, unequal, both predicate `equals` → `CONTRADICTION`
- key match, existing is a range and incoming a point inside it → `REFINEMENT`
- key match, existing NULL value, incoming concrete → `REFINEMENT`
- `forbids` vs `equals` with overlapping value → `CONTRADICTION`
- date/number normalization before comparison (`"2026-09-14"` vs
  `"Sep 14 2026"` must compare equal — put this in a shared coercion helper and
  test it hard, it is a silent-miss source)

### 5.2 `quorum/detect/tier2.py` — bounded LLM adjudicator

```python
def adjudicate(incoming: Claim, existing: Atom) -> Verdict:
    """Bedrock call, temperature 0, strict JSON out, hard timeout.
    Fail closed: on timeout / parse error / throttle -> CONTRADICTION."""
```

Prompt (versioned in `quorum/detect/prompts.py`, do not casually edit):

```
You are a strict consistency checker for an AI agent's memory. You are given two
claims about the same trip. Decide their logical relationship.

CLAIM A (existing, written by role={role_a}): {text_a}
CLAIM B (incoming, written by role={role_b}): {text_b}

Answer with JSON only, no prose:
{"verdict": "agreement"|"refinement"|"contradiction"|"unrelated",
 "confidence": 0.0-1.0,
 "rationale": "<= 20 words"}

Definitions:
- agreement: both can be true and they assert the same thing
- refinement: both can be true; one is strictly more specific
- contradiction: they CANNOT both be true at the same time
- unrelated: they concern different facts

If uncertain, answer "contradiction". A false alarm is safe; a missed
contradiction is not.
```

Enforce: `ADJUDICATE_BUDGET` calls per `remember()`, a global per-run ceiling, a
per-call timeout, and structured logging of latency/tokens for the cost report.

### 5.3 `quorum/policy/rules.py`

Each rule is a pure function `(incoming, existing, verdict, cfg) -> Resolution | None`.
Implement R1–R4 from `CLAUDE.md` §6.3 exactly, evaluated in order by
`engine.resolve()`. Every returned resolution carries `policy_rule` and a
human-readable `rationale` — both are rendered in the dashboard and both are what
make the system explainable rather than magic.

**Done when:** `tests/unit/test_policy.py` covers every rule and every ordering
edge, and each canonical scenario S1–S5 lands on its expected rule.

---

## M6 — Agents and the Atlas Travel domain

### 6.1 Mock inventory — `quorum/domain/inventory.py`

Seeded, deterministic, no network. Flights, hotels, and cars for one city pair
over a two-week window, with prices. Deterministic from `RUN_SEED` so the demo
reproduces exactly.

### 6.2 Agents — LangChain + Bedrock

Five agents, each a thin LangChain agent with a narrow toolset. All memory access
goes through the injected `MemoryClient`; **no agent talks to the database
directly.**

| Agent | Role | Tools | Writes |
|---|---|---|---|
| `flight_agent` | flight_agent (t3) | search_flights, hold_flight | itinerary, arrival date |
| `lodging_agent` | lodging_agent (t3) | search_hotels, hold_hotel | check-in/out dates, hotel |
| `ground_agent` | ground_agent (t3) | search_transfers, hold_transfer | transfer slots |
| `budget_agent` | budget_agent (t2) | — | budget ceiling, policy constraints |
| `research_agent` | research_agent (t4) | — | inferred traveller preferences |
| `booking_agent` | booking_agent (t1) | confirm_booking | confirmed facts (highest authority) |

Every tool that has an external effect routes through `memory.act()` with its
`required_keys` declared. That is what makes the action gate real rather than
decorative.

Agent prompts instruct them to write memory as structured claims:
`(subject_key, predicate, object_text, object_json, confidence)`. Give them a
`remember` tool with a strict schema — do not parse free text into claims, it
adds a failure mode that has nothing to do with your thesis.

### 6.3 Scenarios — `quorum/domain/scenarios/`

One module per scenario S1–S5 (`CLAUDE.md` §8). Each exposes:
```python
def build(workspace_id, seed) -> ScenarioPlan:
    """Deterministic sequence of agent turns + injected contradiction,
    plus expected outcomes per mode for assertion."""
```
S5 needs true concurrency: two agent turns dispatched simultaneously against the
same subject key, with a configurable delay to widen the race window.

**Done when:** each scenario runs headless in all three modes and produces the
divergence table asserted in `tests/scenarios/`.

---

## M7 — Harness, anomaly detection, and the forensic view

### 7.1 `quorum/harness/driver.py`
One driver. Signature: `run(scenario, mode, seed) -> RunReport`. It constructs
the memory client from the factory and knows nothing else about modes (I8).

### 7.2 `quorum/harness/anomaly.py` — post-run detectors

Run these against the final database state:

- **`contradictory_active_pairs`** — the headline metric. Any two `active` atoms
  with the same `subject_key` and unequal `object_json`. Must be **0** in
  `quorum`, non-zero in `naive` and `txn_only`.
- **`lost_updates`** — writes acknowledged to an agent that are absent from final
  state. Non-zero in `naive` only.
- **`stale_reads`** — an action justified by an atom already superseded at read
  time.
- **`wrong_actions`** — actions in `action_log` whose payload contradicts the
  scenario's ground truth. This is the user-visible number.
- **`blocked_actions`** — gate blocks. High in `quorum`, zero elsewhere. Frame
  this as the system working.

### 7.3 `RunReport`
JSON, written to S3 and to `run.report`:
```json
{"run_id":"...","mode":"quorum","scenario":"S5_concurrent_race","seed":1337,
 "anomalies":{"contradictory_active_pairs":0,"lost_updates":0,
              "stale_reads":0,"wrong_actions":0,"blocked_actions":1},
 "conflicts":{"detected":7,"tier1":6,"tier2":1,
              "resolutions":{"supersede":3,"reinforce":2,"contest":1,"reject":1}},
 "performance":{"txn_retries":4,"p50_write_ms":31,"p99_write_ms":118,
                "embed_calls":42,"embed_cache_hit_rate":0.71,
                "adjudicator_calls":1,"est_cost_usd":0.014}}
```

### 7.4 Forensic view — `as_of`
API endpoint `GET /timeline/{run_id}?at=<ts>` runs the memory query
`AS OF SYSTEM TIME` and returns the memory state as it existed. Pair it with
`action_log.justifying_atom_ids` so the UI can answer: *"here is the booking, here
is the exact memory it was made from, at the exact instant it was made."*

**Done when:** a run report renders for all three modes and the timeline endpoint
returns correct historical state for a timestamp 12+ hours old.

---

## M8 — Dashboard (observability, not a chat UI)

Next.js. Four surfaces, in priority order. Build 1 and 2 first; 3 and 4 are
valuable but cuttable.

1. **Three-mode split screen.** Same scenario, three columns. Each column shows
   the resulting booking (right or wrong, colour-coded), the anomaly counters,
   and the wrong-action call-out. This single screen carries the video.
2. **Conflict log.** Live table from `memory_conflict`: incoming vs existing
   claim, detector tier, similarity, verdict, resolution, which policy rule fired
   and why. Explainability made visible.
3. **Memory health.** Transaction retry counter, p50/p99 write latency with the
   semantic layer on vs off, embed cache hit rate, adjudicator call count and
   spend, unmapped-attribute count, contested set size.
4. **Forensic timeline.** A scrubber over the run; dragging it re-queries
   `as_of` and re-renders memory state. Show the action markers on the timeline.

Design constraint: no chat box (`CLAUDE.md` §12). This is an instrument panel.

**Ship a seeded read-only demo workspace** that renders a completed three-mode
comparison on first load, so a judge who never triggers a run still sees
everything.

---

## M9 — MCP Server, AWS deployment, and chaos

### 9.1 Managed MCP Server (auditor persona)
From the CockroachDB Cloud Console, copy the MCP config snippet for the cluster
and connect it to Claude Code using the **read-only** `auditor` role. Document in
`quorum/mcp/README.md`:
- exact config snippet (with credentials redacted)
- that it is read-only by default with audit logging — a safety property, not a
  limitation
- `quorum/mcp/queries.md`: curated auditor questions to run live on camera, e.g.
  *"show every contested memory in this workspace and the action it blocked"* and
  *"which agent wrote the atom that justified booking X?"*

Rehearse these. An unrehearsed live MCP query on camera is a coin flip.

### 9.2 AWS
- **Lambda** — one handler per agent turn; the swarm is a fan-out invocation.
  This is what makes concurrency real rather than simulated with threads, and it
  is worth saying so explicitly.
- **Bedrock** — Titan v2 embeddings + Claude for agents and the adjudicator.
- **S3** — run reports, traces, and scenario artifacts under one prefix.
- **CloudWatch** — export `txn_retries`, `contradictions_detected`,
  `blocked_actions` as custom metrics. A CloudWatch graph in the video reads as
  production-grade.
- **IAM** — least privilege: Bedrock invoke + S3 put on one prefix. Nothing else.

### 9.3 Chaos
Either a multi-region CockroachDB cluster or a local 3-node docker cluster. Mid
swarm-run, kill a node. Assert: writes continue, retry counter rises and settles,
`contradictory_active_pairs` stays 0. Twenty seconds of video, disproportionate
credibility.

---

## 10. Verification checklist (gate before recording)

**Correctness**
- [ ] `contradictory_active_pairs == 0` in `quorum` across all five scenarios
- [ ] `contradictory_active_pairs > 0` in **both** `naive` and `txn_only` on S1, S3, S5
- [ ] `wrong_actions > 0` in `txn_only` — the pivot of the whole argument
- [ ] `test_txn_isolation.py` green over 100 consecutive runs
- [ ] at least one scenario resolves to CONTEST and visibly blocks an action

**Integrity of the comparison**
- [ ] one workload driver, one seed, one agent implementation across modes
- [ ] no `if mode ==` outside `factory.py` (CI lint)
- [ ] `naive` documented as a fair baseline in `docs/CONSISTENCY_MODEL.md`

**Production readiness**
- [ ] four distinct DB roles; auditor is read-only; `agent_writer` has no DELETE grant
- [ ] cross-workspace read returns zero rows (negative test)
- [ ] node-kill run completes with zero anomalies
- [ ] retry counts bounded and surfaced, never hidden
- [ ] no secrets anywhere in git history (`git log -p | grep` for connection strings)

**Submission mechanics**
- [ ] Apache-2.0 visible in GitHub About sidebar
- [ ] demo URL live, seeded, works in a private browser window
- [ ] README runs cold: setup → seed → `make demo-s5` in under 10 minutes
- [ ] architecture diagram committed
- [ ] `docs/SUBMISSION.md` has the tool-by-tool writeup drafted
- [ ] video under 3:00 and shows the memory layer, not just the app

---

## 11. Demo video shot list (target 2:50)

| Time | Shot | Line to land |
|---|---|---|
| 0:00–0:15 | Atlas Travel swarm planning a trip, four agents in parallel | "Four agents. One shared memory." |
| 0:15–0:40 | `naive` mode: hotel booked for the wrong night. Show the two contradictory atoms sitting side by side, both `active`. | "Both facts committed. The agent believed both." |
| 0:40–1:10 | `txn_only`: serializable, zero lost updates, retry counter visible — **and the same wrong booking** | "This is CockroachDB, used correctly. Isolation is necessary. It is not sufficient." |
| 1:10–1:55 | `quorum`: contradiction detected in-transaction, policy rule shown, one atom superseded; S3 escalates to CONTEST and the gate **blocks the booking** | "The check happens inside the transaction that commits the write. That is only sound because it is serializable." |
| 1:55–2:20 | MCP auditor in Claude Code: query contested memory and the blocked action; forensic scrubber back to the decision instant | "Read-only, audit-logged, and we can replay exactly what it knew." |
| 2:20–2:40 | Kill a node mid-run; writes continue; anomalies stay 0 | "Memory that does not go offline." |
| 2:40–2:50 | Architecture diagram + tool/service callouts | Name the three CockroachDB tools and four AWS services on screen. |

The `txn_only` beat at 0:40 is the most important thirty seconds in the
submission. It is what separates you from every other entrant who will show
"vector store bad, CockroachDB good."

---

## 12. `docs/SUBMISSION.md` — draft as you build

Devpost asks explicitly what each tool did. Answer concretely, in the agent's
voice:

- **Distributed Vector Indexing** — "the agent searches the semantic
  neighbourhood of every claim it is about to write, inside the same transaction
  that commits it, to find claims it would contradict."
- **Managed MCP Server** — "a human auditor connects Claude Code to the cluster
  read-only and interrogates contested memory and blocked actions live; every
  query is audit-logged."
- **ccloud CLI** — "provisions the cluster, creates four least-privilege service
  accounts mapped to agent authority tiers, and pulls SQL audit logs into the
  observability dashboard."
- **Bedrock** — "Titan v2 embeds every claim; Claude runs the agent swarm and
  serves as the bounded tier-2 contradiction adjudicator."
- **Lambda** — "each agent turn is a Lambda invocation; the swarm is a genuine
  concurrent fan-out, which is what makes the race conditions real."
- **S3 / CloudWatch** — "run reports and traces; retry and contradiction counts
  exported as custom metrics."

---

## 13. Cost control

- Embedding cache on disk during development — dominant cost saver.
- Cap `ADJUDICATE_BUDGET` and set a global per-run ceiling.
- Scenario runs use temperature 0 and short max-tokens.
- Free-tier CockroachDB is the target; track and publish per-run cost in the
  report. A stated cost number is a Product Readiness signal.

---

## 14. Fallbacks and kill criteria

Decide these **early**, not in the final week.

| If this fails | Fallback |
|---|---|
| `CREATE VECTOR INDEX` unavailable on your tier/version | Brute-force cosine over a bounded candidate set filtered by `workspace_id`. The consistency argument is untouched. Say so plainly in the README — an honest, measured limitation reads better than a hidden one. |
| `AS OF SYSTEM TIME` window too short | Drop the forensic scrubber to a stretch goal; keep the append-only history and render the timeline from `valid_from`/`valid_to` instead. |
| Bedrock model access not granted in time | Any embedding model you can reach + a smaller adjudicator. Tier 1 carries most detection anyway — this is exactly why tier 1 is deterministic. |
| Lambda fan-out eats too much time | Run the swarm as local processes with true concurrency. Note it as deployment-pending. Concurrency is what matters; Lambda is how you got it. |
| **M2 gate fails outright** | Pivot to the "Commit" idea: atomic action + memory in one transaction, with the `kill -9` demo. Same database argument, simpler proof, and most of M0–M4 carries over. Make this call by **August 1**. |

The dashboard, chaos test, and forensic view are cuttable in that order. The
three-mode comparison, the action gate, and `test_txn_isolation.py` are not
cuttable — they are the submission.
