# Quorum

**A memory consistency layer for multi-agent systems, built on CockroachDB.**

> Transactions solve *write* conflicts. They do not solve *semantic* conflicts.
>
> Two agents can write mutually contradictory facts as two different rows, both
> commit cleanly under SERIALIZABLE, and the swarm now holds memory that is
> internally inconsistent — and will produce a wrong action. Quorum detects
> semantic contradiction **inside the transaction that commits the write**,
> resolves it under an explicit policy, and refuses to let a downstream action
> execute against contested memory.

Licence: Apache-2.0.

---

## The result, up front

The same workload, the same seed, the same agents. The only thing that changes
is the memory layer.

```
scenario S5 — two agents write contradictory check-in dates simultaneously

mode       contradictory pairs   wrong actions   blocked   40001 retries
naive      1                     1               0         0
txn_only   1                     1               0         0
quorum     0                     0               1         1
```

`txn_only` is the important row. It is CockroachDB used correctly —
serializable, zero lost updates, zero dirty reads, zero write skew — and it
still ends up holding two mutually contradictory facts, because the
contradiction lives across two structurally unrelated rows and no isolation
level has an opinion about semantics. **Isolation is necessary. It is not
sufficient.**

The proof spike ran that race 200 times per mode against a live cluster:

| mode | contradictory pairs | rate | 40001 retries |
|---|---|---|---|
| naive | 200 | 100.0% | 0 |
| txn_only | 200 | 100.0% | 0 |
| quorum | **0** | **0.0%** | 200 |

Full numbers: [`spikes/results.json`](spikes/results.json). A control run with
the artificial race window removed entirely
([`spikes/results_delay0.json`](spikes/results_delay0.json)) produced identical
outcomes — the widening is disclosed, and it is not what causes the failure.

---

## Run it cold

Needs Python 3.10+ and a CockroachDB cluster. **No AWS account required** — the
repo runs end to end with deterministic offline stand-ins for embeddings and the
tier-2 adjudicator, and every run report records which provider produced it, so
nothing can be mistaken for a Bedrock result.

```bash
git clone <repo> && cd Quorum
pip install -r requirements.txt

cp .env.example .env
# put your CockroachDB Cloud connection string in CRDB_URL

python -m quorum.db.migrate           # schema, indexes, GC TTL, agent registry
python -m quorum.harness.report --all # all 5 scenarios x 3 modes
```

Type your own contradictions and watch the memory layer react — nothing in this
console is scripted, and `mode` swaps the memory layer under the same data:

```bash
make console
# > remember lodging_agent hotel.checkin_date = 2026-09-14
# > remember booking_agent hotel.checkin_date = Sep 15 2026
#   SUPERSEDE  detected contradiction via tier-1 structural
#   rule R1: booking_agent (tier 1) outranks lodging_agent (tier 3)
# > mode naive     ... then enter the same two claims and compare
```

Reproduce the flagship race on its own:

```bash
make demo-s5
# or: python -m quorum.harness.report --scenario S5_concurrent_race --delay-ms 40
```

Run the original proof spike:

```bash
python spikes/bootstrap.py            # verifies CREATE VECTOR INDEX and GC TTL
python spikes/prove_race.py --iterations 200 --delay-ms 50
```

Dashboard:

```bash
cd dashboard && npm install && npm run dev     # http://localhost:3000
```

It ships with a baked snapshot of a completed three-mode comparison, so it
renders the whole story on first load with no database and no credentials.

---

## The tests are the argument

```bash
make test          # unit + scenarios, no cluster needed for the unit half
make test-isolation   # the flagship, 100 consecutive races
make lint          # asserts no mode branching outside factory.py (I8)
```

- **[`tests/integration/test_txn_isolation.py`](tests/integration/test_txn_isolation.py)** —
  two threads write contradictory atoms concurrently; assert memory is left with
  exactly one active atom or a contested pair, never two active contradictory
  atoms. This test *is* the thesis. It ships with a companion test that asserts
  `naive` **does** produce the forbidden state under identical conditions —
  without which the flagship could be passing because the race never happens.
- **[`tests/scenarios/`](tests/scenarios/)** — S1–S5 across all three modes,
  asserting the *divergence*. If `txn_only` ever matches `quorum`, these fail loudly.
- **[`tests/unit/`](tests/unit/)** — key normalization, every tier-1 branch,
  every policy rule and ordering edge, cross-workspace isolation.

---

## How it works

```
Phase A — prepare (OUTSIDE any transaction, no locks held)
    embed the claim · probe the semantic neighbourhood
    tier-1 structural classify · bounded tier-2 adjudication where needed

Phase B — commit (INSIDE one serializable transaction)
    re-read the neighbourhood AUTHORITATIVELY
    reconcile against the probe · resolve via the policy engine
    insert / supersede / contest · log every detection
```

Every network call is in Phase A, so no transaction is ever held open across
unbounded latency. Every decision that counts is made in Phase B, where the
neighbourhood read and the write are atomic.

**The probe is advisory. The in-transaction read is authoritative.** If a
concurrent writer changed the neighbourhood in between, either our
in-transaction read sees it, or the transaction fails with 40001 and retries
into a world where it does. There is no window where two contradictory facts
both slip through.

### Resolution policy

Ordered rules, first match wins, each a pure function:

| rule | fires when | outcome |
|---|---|---|
| R1 authority | writers have different authority tiers | supersede / reject |
| R2 evidence | corroboration differs by `EVIDENCE_MARGIN` | supersede / reject |
| R3 recency | same tier, newer claim materially more confident | supersede |
| R4 contest | otherwise | **mark both contested, block dependent actions** |

R4 is not a failure mode, it is the safety net. A system that declines to guess
and escalates to a human is more trustworthy than one that always has an answer.

### Architecture

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). The consistency guarantees,
the fairness of the baseline, and every deliberate deviation from spec are in
[`docs/CONSISTENCY_MODEL.md`](docs/CONSISTENCY_MODEL.md) — read that one before
deciding whether to believe the numbers.

---

## Canonical scenarios

| id | contradiction | tier | quorum resolution | failure if unguarded |
|---|---|---|---|---|
| `S1_checkin_date` | lodging plans Sep 14; booking confirms Sep 15 | 1 | supersede via R1 | hotel booked for the wrong night |
| `S2_budget_ceiling` | $2,400 ceiling vs inferred "flexible on price" | 2 | reject via R1 | booking exceeds policy |
| `S3_ground_overlap` | two ground agents, one transfer slot | 1 | **contest via R4** | double-booked transfer |
| `S4_preference_reversal` | prefers email, then opts out | 2 | supersede via R3 | emails after opt-out |
| `S5_concurrent_race` | contradictory dates written **simultaneously** | 1 | **contest**, after a real 40001 | memory holds two truths |

`S2` requires real semantic embeddings — its claims share no subject key, so
only ANN over a true embedding space can surface the pair. Without Bedrock it is
reported `NOT TESTED` rather than counted either way.

---

## What is used, and what it does

**CockroachDB**

- **Distributed vector indexing** — the agent searches the semantic
  neighbourhood of every claim it is about to write, inside the same transaction
  that commits it, to find claims it would contradict.
- **Managed MCP Server** — a human auditor attaches Claude Code to the cluster
  read-only and interrogates contested memory and blocked actions live. Setup and
  rehearsed queries: [`quorum/mcp/`](quorum/mcp/).
- **ccloud CLI** — provisions the cluster and four least-privilege roles mapped
  to agent authority tiers: [`infra/ccloud/provision.sh`](infra/ccloud/provision.sh).
  The `agent_writer` role is never granted `DELETE`, so the swarm **physically
  cannot erase a claim** — supersession preserves history because the agent has
  no other option. (CockroachDB has no column-level privileges, so the narrower
  "UPDATE only these five columns" restriction is enforced in the supersede path
  in application code, not by the grant. See
  [`quorum/mcp/queries.md`](quorum/mcp/queries.md) §7.)

**AWS** — Bedrock (Titan v2 embeddings, Claude as the bounded tier-2
adjudicator), Lambda (one invocation per agent turn), S3 (run reports),
CloudWatch (retry and contradiction counts as custom metrics).

Full write-up: [`docs/SUBMISSION.md`](docs/SUBMISSION.md).

---

## Status

Built and verified against CockroachDB v26.2.1:

- schema, indexes, GC TTL, four-role RBAC script
- all three memory clients, detection tiers, policy engine
- five scenarios, single mode-parameterised driver, anomaly detectors
- 107 tests passing, flagship isolation test green
- read-only FastAPI surface and static dashboard

Not exercised in this environment: Bedrock, Lambda, S3 and CloudWatch (no AWS
credentials configured), and the node-kill chaos test (needs a local Docker
cluster — scripts in [`infra/chaos/`](infra/chaos/)). Those paths are
implemented and documented; they have not been run here, and the docs say so
rather than implying otherwise.
