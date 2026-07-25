# Submission write-up

Devpost asks explicitly what each tool did. Answered concretely, in the agent's
voice, with a pointer to the code that does it.

---

## What Quorum is

A memory consistency layer for multi-agent systems. It detects semantic
contradiction between agent memories **inside the transaction that commits the
write**, resolves it under an explicit authority policy, and refuses to let a
downstream action execute against contested memory.

The demo domain is *Atlas Travel*: a concierge swarm planning one trip with
parallel specialist agents. The domain exists so that a memory inconsistency
produces a **visibly wrong action** — a hotel booked for the wrong night, a
transfer double-booked, an email sent after an opt-out.

---

## CockroachDB tools

### Distributed vector indexing

*What the agent does with it:* before writing any claim, the agent searches the
semantic neighbourhood of that claim — an ANN search over
`VECTOR(1024)` embeddings — **inside the same serializable transaction that
commits the write**, to find claims it would contradict.

This is the reason the project is on CockroachDB and not on a vector database
plus a relational database. Contradiction detection is a read-modify-write, and
a read-modify-write is only sound under serializable isolation. The read is a
vector search. So the vector index and the transactional rows have to be in the
same transactional domain — with embeddings in Pinecone and rows in Postgres,
the operation cannot be made atomic at all.

- `sql/002_indexes.sql` — `CREATE VECTOR INDEX idx_atom_embedding ON memory_atom (workspace_id, embedding)`, a C-SPANN index with a prefix column
- `quorum/memory/base.py::_neighbourhood` — the ANN search, unioned with an exact `subject_key` lookup so a structural match is never missed to ANN recall
- `quorum/memory/quorum.py::_commit` — that search running inside `run_txn`

*Measured, not assumed:* `tools/bench_vector_index.py` seeds 10,000 atoms and
reports the plan, the latency and the recall. The first run found the optimiser
ignoring the index entirely — the ANN and brute-force plans were identical —
because `status IN (...)` inside the ANN subquery is a predicate the index
cannot cover. Making the index partial on `valid_to IS NULL` and moving the
status filter outside the subquery fixed it: **2.5x faster than a forced scan
at p50**, with recall@8 of 77% at `vector_search_beam_size = 64`, where the
8th-nearest neighbour is only 0.16% farther than exact. Full write-up in
`docs/CONSISTENCY_MODEL.md` §8.

### Cloud Managed MCP Server

*What the agent does with it:* a human auditor attaches Claude Code to the
cluster with a **read-only** role and interrogates memory state, the conflict
log, and blocked actions live — "show every contested memory in this workspace
and the action it blocked", "which agent wrote the atom that justified this
booking?", "what did memory hold 30 seconds before the booking?".

Read-only with audit logging is the point, not a limitation: an auditor that can
write is not an auditor. The setup includes a verification step that the account
**fails** to `UPDATE`, because an unverified read-only claim is just a claim.

- `quorum/mcp/README.md` — setup and the read-only verification
- `quorum/mcp/queries.md` — the rehearsed queries

### ccloud CLI

*What the agent does with it:* provisions the cluster and creates **four
least-privilege SQL roles mapped to agent authority boundaries** — `agent_writer`
(read + append memory), `gate_service` (the only writer of `action_log`),
`auditor` (read-only, for MCP), `quorum_admin` (migrations) — then pulls SQL
audit logs.

The detail worth pointing at: `agent_writer` is granted `UPDATE` on exactly five
columns — `valid_to`, `superseded_by`, `status`, `evidence_count`, `confidence`.
It physically cannot rewrite what a claim said. Append-only memory is enforced as
a **grant**, not as a convention in application code.

- `infra/ccloud/provision.sh`

---

## AWS services

### Bedrock

- **Titan v2** embeds every claim (`subject_key + predicate + object_text`) into
  the `VECTOR(1024)` column that the neighbourhood search runs over.
  `quorum/embed/bedrock.py`, with a content-hash disk cache (`quorum/embed/cache.py`)
  because agents re-assert the same claims constantly.
- **Claude** is the bounded tier-2 contradiction adjudicator — used only for
  pairs that the deterministic tier-1 classifier could not decide and that sit
  above a similarity threshold. Temperature 0, strict JSON, hard timeout, hard
  call budget, and it **fails closed to `contradiction`** on timeout, parse
  failure or throttle. A false contest is safe and visible; a missed
  contradiction is the exact failure this project exists to prevent.
  `quorum/detect/tier2.py`, prompt versioned in `quorum/detect/prompts.py`.

The LLM is deliberately a *classifier of last resort*, never the resolver.
Resolution is rule-based and explainable (`quorum/policy/rules.py`).

### Lambda

One invocation per agent turn; the swarm is a fan-out. This is what makes
concurrency genuine rather than simulated. `infra/lambda/`.

### S3

Run reports, traces and scenario artifacts under one prefix.
`quorum/harness/report.py::maybe_upload_s3`.

### CloudWatch

`txn_retries`, `contradictions_detected` and `blocked_actions` exported as
custom metrics. `infra/lambda/metrics.py`.

---

## What was actually run, and what was not

Stated plainly because a submission that overclaims is worse than one that
scopes honestly.

**Verified against a live CockroachDB v26.2.1 Cloud cluster:**

- `CREATE VECTOR INDEX` works with the documented syntax; `VECTOR(1024)` column,
  C-SPANN index with prefix column, ANN query with `<->`
- GC TTL raised to 90000s and `AS OF SYSTEM TIME` forensic reads confirmed
- the 200×3 proof spike, twice, plus a control run with the race window removed
- all five scenarios in all three modes
- 107 tests passing; the flagship isolation test green over repeated races with
  real, counted 40001 retries
- read-only FastAPI surface and the static dashboard, rendered and checked

**Implemented but NOT run in this environment:**

- Bedrock, Lambda, S3, CloudWatch. Credentials were configured and the control
  plane works — 122 models enumerate — but the account has no runtime
  entitlement, so **every** model refuses to invoke: 22 chat models across
  Anthropic, Mistral, Qwen, Google, Meta, Nova, DeepSeek and AI21, and 8
  embedding models, all `Operation not allowed`. Provider selection now proves
  reachability with a real invocation before claiming a provider, and every run
  report records which one produced it.
- **Embeddings run locally instead** (`quorum/embed/local.py`, ONNX
  bge-small-en-v1.5, zero-padded 384 -> 1024 so cosine is preserved exactly).
  That is a genuine semantic space, which is why S2 — whose conflicting claims
  share no subject key — now passes rather than being reported untested.
- Tier-2 adjudication still fails closed. See `docs/CONSISTENCY_MODEL.md` §7 for
  exactly what that does and does not prove.
- The node-kill chaos test — CockroachDB Cloud Basic gives no node to kill, and
  the local Docker cluster needs the Docker daemon running.
  `infra/chaos/start_cluster.sh` + `tests/chaos/`.
- `ccloud` provisioning — the CLI was not installed; the cluster used here was
  created through the console. The script is written and idempotent.

`S2_budget_ceiling` is the cleanest evidence that the vector index is
load-bearing. Its two claims share no subject key, so tier 1 structurally
cannot reach them and only ANN over a real embedding space surfaces the pair —
measured at 0.8633 cosine against 0.7461 for an unrelated pair. Under the
synthetic stand-in it was reported NOT TESTED; with a real model it passes.
Exactly one scenario depends on that capability, and it is the one tier 1
cannot reach.

---

## Judging criteria

**Agentic memory design.** Memory is append-only atoms with attribution,
confidence, evidence count and validity intervals. Contradiction is a
first-class state (`contested`), not an error. The system can say "I do not
know which of these is true, so I will not act" — and the action gate makes that
refusal consequential rather than advisory.

**Technological implementation.** The two-phase write path satisfies two
invariants that pull against each other: no network calls inside a transaction,
and the neighbourhood read in the same transaction as the write. The probe /
authoritative-read reconciliation is what makes that sound, with a bounded
re-prepare that fails closed.

**Real-world impact.** Every multi-agent framework shipping today writes shared
memory with no contradiction control. The failures are not hypothetical: wrong
booking, policy breach, contacting someone after they opted out.

**Product readiness.** Four least-privilege roles, a read-only audited auditor
path, no `DELETE` grant to the agent role so memory cannot be erased at the
database level, bounded and surfaced retries, cost and latency measured per run, negative tests
for cross-workspace leakage, and a CI lint that fails the build if the three
modes ever stop being the same experiment.

**Creativity.** The `txn_only` mode. Most entries will show "vector store bad,
CockroachDB good". This one includes a column that is CockroachDB used perfectly
and *still wrong*, because that is the only way to show the gap being filled is
real.
