# Consistency model

What Quorum guarantees, what it does not, how the baseline was chosen, and every
place the implementation departs from the spec. If you only read one document to
decide whether to believe the numbers, read this one.

---

## 1. The claim

> Two agents can write mutually contradictory facts as two different rows, both
> commit cleanly under SERIALIZABLE, and the swarm now holds memory that is
> internally inconsistent and will produce a wrong action.

Serializability constrains the **order** of operations. It says nothing about
the **meaning** of the data those operations write. `INSERT (check-in, Sep 14)`
and `INSERT (check-in, Sep 15)` touch different rows, violate no constraint, and
are trivially serializable in either order. The database is working perfectly and
the memory is wrong.

Quorum closes that gap by making contradiction detection part of the transaction
that commits the write.

---

## 2. Why the check must be inside the transaction

Detection is a read-modify-write: read the semantic neighbourhood, decide, write.

Split the read from the write and two concurrent writers each read a
neighbourhood that does not yet contain the other, each conclude "no conflict",
and both commit. The checker passes and the corruption is **worse than having no
checker**, because now the memory is trusted.

The neighbourhood read is an ANN vector search. So the vector index and the
transactional rows must live in the same transactional domain. With embeddings
in Pinecone and rows in Postgres this operation cannot be made atomic at all —
there is no transaction that spans both systems. CockroachDB provides
distributed serializable transactions over vectors and rows simultaneously,
which is why it is load-bearing here rather than incidental.

### Measured, not asserted

`spikes/prove_race.py` ran the two-writer race 200 times per mode against a live
CockroachDB v26.2.1 cluster:

| mode | contradictory pairs | rate | 40001 retries | p50 ms |
|---|---|---|---|---|
| naive | 200 | 100.0% | 0 | 153.6 |
| txn_only | 200 | 100.0% | 0 | 173.0 |
| quorum | **0** | **0.0%** | 200 | 491.4 |

Final state per iteration: naive and txn_only ended with two active atoms every
single time; quorum ended with exactly one, 200 times out of 200. Exactly one
40001 per race — the loser's read is invalidated by the winner's commit, it
retries, re-reads, and resolves.

**Disclosure.** The spike takes a `--delay-ms` flag that widens the read→write
window. A control run at `--delay-ms 0`, with no widening at all, produced
identical outcomes (`spikes/results_delay0.json`): naive still failed 100% of the
time. The delay is not load-bearing for the result; a cloud round-trip already
exceeds it.

**One honest caveat.** `txn_only`'s 100% is analytically certain, not empirical:
it performs no check, so it must fail. Do not present it as a discovery. The
falsifiable results are naive's failure and quorum's zero.

---

## 3. What `naive` actually does, and why it is a fair baseline

If the baseline is unfair the whole comparison is worthless, so here is exactly
what it is.

`naive` is what a competent engineer actually builds on a vector store:

- embed the claim, ANN-search for similar memories
- **it does run a conflict check** — an exact-value dedup on the same attribute,
  which is the dedup everyone writes
- otherwise append
- read and write are **separate autocommitted operations**

What it does not have — because these are the novel parts of this project, not
oversights — is an authority-tier policy engine, supersession, a contested
state, or an action gate. A vector memory store has no notion of which agent
outranks which.

Two independent failure modes follow, and the harness distinguishes them:

1. **Semantic** — different values are not duplicates, so both get appended.
   Happens with no concurrency at all.
2. **Race** — the read and the write are not atomic, so even the dedup check
   misses a writer that lands in between.

**We give the baseline the best case it could possibly have.** Its vector index
and its rows are the same CockroachDB database, so it does not pay the
cross-store replication lag a real Pinecone + Postgres deployment would. A real
separate-store deployment is strictly worse than what is measured here.

### Two baselines, deliberately

`spikes/prove_race.py` uses a *stronger* naive than the product does: there,
naive shares the **full** detection and resolution code with quorum, and the
only difference is `autocommit=True` versus `run_txn`. That isolates one
variable — the transaction — and answers "is the transaction necessary?" (yes:
even a naive with complete detection fails).

The product's `naive` (`quorum/memory/naive.py`) is the realistic stack and
answers a different question: "does what people actually build fail?" (yes,
and for two reasons rather than one).

Both are documented because they answer different questions. Neither is the
strawman.

---

## 4. What `txn_only` is

CockroachDB used exactly as designed. Same storage as quorum, same serializable
transactions, same retry wrapper. Zero lost updates, zero dirty reads, zero
write skew. Every write is a plain `INSERT` with no classification and no policy.

This is the most important column in the project. It is the answer to "isn't this
just the database working as designed?" — it *is* the database working as
designed, and it still produces the wrong booking.

---

## 5. Guarantees

**Quorum guarantees** (within one workspace):

- No two `active` atoms with the same `subject_key` and different `object_json`
  survive a completed write. Asserted by `tests/integration/test_txn_isolation.py`.
- Every detection is recorded in `memory_conflict`, including benign ones.
- Memory is append-only. Supersession sets `valid_to` and `superseded_by`; there
  is no `DELETE` in the write path, and the `agent_writer` grant makes that a
  database-level guarantee rather than a convention.
- An action whose required keys are contested, missing, or ambiguous is blocked
  and logged.
- Contested atoms are returned by `recall()` and flagged, never dropped.

**Quorum does not guarantee:**

- **Cross-key contradictions without semantic similarity.** If two claims
  contradict but share no subject key *and* their embeddings are not close, no
  candidate pair is generated and nothing is detected. This is a recall problem,
  not a soundness problem, and it is bounded by `ANN_K` and `TAU_ADJUDICATE`.
- **Correct adjudication of genuinely ambiguous language.** Tier 2 is an LLM
  classifier and it is wrong sometimes. It fails closed to `contradiction`, so
  its errors produce false contests (safe, visible, human-resolvable) rather
  than missed contradictions.
- **Anything across workspaces.** Scoping is enforced per query and tested
  negatively, but there is no cross-workspace reasoning by design.
- **That the chosen value is *true*.** Quorum makes memory internally consistent
  and attributable. It cannot make an agent's claim correct.

---

## 6. Deliberate deviations from the spec

Recorded here because a silent deviation is indistinguishable from a bug.

### 6.1 Rule R3 requires a strict improvement

`CLAUDE.md` §6.3 writes R3 as
`confidence_incoming >= confidence_existing - CONF_EPSILON`. That fires whenever
confidences are equal, which makes **R4 unreachable** for two same-tier writers
with identical evidence and confidence — exactly the S3 case that §8 says must
resolve to CONTEST, and exactly what §6.3's own instruction ("tune the
thresholds so at least one canonical scenario lands in R4") asks for.

Implemented as `confidence_incoming > confidence_existing + CONF_EPSILON`.
S4 (0.80 vs 0.60) supersedes on recency; S3 (0.70 vs 0.70) falls through to
CONTEST. Both §8 expectations hold.

### 6.2 The action gate lives only in `quorum`

`CLAUDE.md` §6.5 says the gate "still runs" in naive and txn_only but passes.
Taken literally, the ambiguity check (`len(active) > 1 → BLOCK`) would fire in
naive too, and naive would refuse the wrong booking — flattering the baseline
with a safety property it would never actually have.

An action gate is not something a vector store gives you; it is Quorum's
contribution. So `MemoryClient.act()` defaults to the **ungated** behaviour a
normal agent stack has (recall, take the most recent answer, act), and
`QuorumMemory` overrides it with the real gate. Naive and txn_only are measured
as the systems they actually are.

### 6.3 `wrong_actions` is not decided by a coin flip

An early implementation scored an action wrong only if the value it acted on
disagreed with ground truth. But when memory holds two contradictory answers,
which one the agent picks is arbitrary — naive scored 0 wrong actions on S1
purely because "most recent" happened to be correct.

An action is now wrong if **any** of these hold:

1. a required key had more than one live active answer at action time
   (*ambiguous memory* — booking a hotel while memory says both Sep 14 and
   Sep 15 is wrong even if you guess the right night; you had no basis),
2. the value acted on contradicts declared ground truth,
3. the payload violates a numeric constraint held in memory (S2's budget).

Test (1) is the primary one precisely because it does not depend on luck.

### 6.4 `run_id` is nullable

`memory_conflict.run_id` and `action_log.run_id` were specified `NOT NULL`. That
makes the memory client crash when used outside a harness run — and in
production there is no "run". Relaxed in `sql/005_run_id_nullable.sql`.

---

## 7. Running without Bedrock

The repository runs end to end with no AWS account, using deterministic offline
stand-ins. **Every run report records which provider produced it**
(`providers.embedder.provider`, `providers.tier2.provider`), so a result can
never be mistaken for one produced by real models.

| | with Bedrock | offline |
|---|---|---|
| embeddings | Titan v2, real semantic space | hash-based vectors grouped by `subject_key` |
| tier 2 | Claude classifies the pair | **fails closed** to `contradiction` |

What the offline mode still proves, because none of it depends on the model:
the serializable write path, tier-1 structural detection, the policy engine,
supersession, contest, the action gate, retry behaviour, and the forensic view.

What it cannot prove: detection of contradictions between claims that do **not**
share a subject key. The offline embedder places distinct subject keys
near-orthogonal by construction, so no candidate pair is ever generated.
`S2_budget_ceiling` is exactly that case, and it is reported as `NOT TESTED`
rather than counted as passing or failing — the experiment could not be
performed. That limitation is also the cleanest demonstration that the vector
index is load-bearing: remove real embeddings and precisely one scenario stops
working, and it is the one tier 1 cannot reach.

Note that with the offline stub, `S4_preference_reversal` reaches the correct
resolution through the fail-closed path rather than through genuine semantic
judgement. It gets the right answer for the wrong reason. With Bedrock it is a
real classification.

---

## 8. Known limits of the vector index

`sql/002_indexes.sql` declares `CREATE VECTOR INDEX idx_atom_embedding ON
memory_atom (workspace_id, embedding)` — a C-SPANN index with `workspace_id` as
a prefix column, verified to create successfully on v26.2.1.

**At demo scale the planner does not use it.** With a handful of rows per
workspace, `EXPLAIN` shows a full scan plus top-k, which is a correct
cost-based decision, not a capability gap. Detection at this scale is carried by
the exact `subject_key` branch of the neighbourhood query — which is exactly why
`BUILD.md` §4.6 insists on unioning the exact lookup rather than relying on ANN
alone.

The honest statement: the vector index is necessary for the general case
(cross-key semantic contradiction, S2) and is provably correct, but this
submission has not measured it doing work at a scale where the planner prefers
it. That measurement is the first thing to do with more time.

---

## 9. Tuning knobs and their tradeoffs

| knob | default | tradeoff |
|---|---|---|
| `ANN_K` | 8 | higher finds more candidate conflicts, costs latency on every write |
| `TAU_ADJUDICATE` | 0.82 | lower escalates more pairs to tier 2: better recall, more cost and latency |
| `ADJUDICATE_BUDGET` | 3 | caps tier-2 calls per `remember()`; exceeding it fails closed |
| `EVIDENCE_MARGIN` | 2 | how much corroboration must differ before R2 fires |
| `CONF_EPSILON` | 0.05 | how much more confident a newer claim must be for R3 |
| `TXN_MAX_RETRIES` | 8 | beyond this a write fails loudly and is counted; it is never dropped silently |
| `REPREPARE_MAX` | 2 | re-probe attempts when a new unjudgeable neighbour appears, then fail closed to CONTEST |

---

## 10. Cost of consistency

Measured on the canonical scenarios (offline embedder, so embedding cost is
zero; the transaction cost is real):

- `quorum` p50 write latency runs roughly 2–4× the baselines. Most of it is the
  in-transaction neighbourhood read plus retries under genuine contention.
- Retries are bounded, counted, and surfaced. `txn_give_ups` is 0 across every
  run recorded here; if it were not, the write would have failed loudly.

`CLAUDE.md` §9 targets under 25 ms of work inside any transaction. The scenario
runs deliberately exceed that because the disclosed race-widening delay sits
inside the transaction. Transactions slower than `TXN_SLOW_MS` are recorded in
`performance.slow_txns`.
