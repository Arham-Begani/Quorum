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

## 7. Embedding and adjudication providers

Two independent choices, and every run report records both
(`providers.embedder.provider`, `providers.tier2.provider`) so a result can
never be mistaken for one produced by different machinery.

### Embeddings

| provider | what it is | cross-key detection |
|---|---|---|
| `bedrock_titan` | Titan v2 via AWS | yes |
| `local_onnx` | BAAI/bge-small-en-v1.5 through ONNX on CPU | **yes** |
| `synthetic_offline` | hash-based vectors, NOT a model | **no** |

The property that matters is `is_semantic`, not "is it cloud". The first two
place semantically related claims near each other; the third cannot, by
construction, so a scenario that needs that capability is reported **untested**
rather than passed or failed while it is in use.

The local model emits 384 dimensions and the column is `VECTOR(1024)`. Rather
than migrate, we **zero-pad**, which is exact rather than approximate:
appending zeros changes neither the norm nor any dot product, so cosine and L2
distance between padded vectors are identical to the originals. The index, the
distance operator and `TAU_ADJUDICATE` all keep working untouched.

Measured on the S2 pair, whose claims share no subject key:

```
cos(budget ceiling $2400, traveller flexible above $2400) = 0.8633   <- the pair
cos(budget ceiling $2400, flight number AT103)            = 0.7461   <- unrelated
TAU_ADJUDICATE = 0.82
```

That margin is real but not generous. `bge-small` compresses everything into a
fairly narrow band, so the gap between a genuine semantic pair and an unrelated
one is about 0.12. Titan v2 separates better. If `TAU_ADJUDICATE` were raised
to 0.87 the S2 pair would stop being escalated at all.

### Provider selection is proven, not assumed

Selection runs a **real invocation** before claiming a provider. Credentials
existing is not the same as the service working: a valid IAM key on an account
without Bedrock entitlement authenticates perfectly and then refuses every
call. Selecting on credentials alone reported `bedrock_titan` in the run report
while nothing was actually being embedded — every write failed and every action
blocked on missing memory. The probe costs one call at start-up and the failure
reason is recorded in the report rather than swallowed.

### What the fail-closed adjudicator does and does not prove

With no reachable model, tier 2 returns `contradiction` for every pair it is
asked about. That is the specified failure behaviour, not a simulation of
judgement, and it has a consequence worth stating plainly:

**S2, S3 and S4 currently reach their documented resolutions with a real
detection step and a fail-closed verdict.** The embeddings genuinely surface
the candidate pair — S2's 0.8633 similarity across two different subject keys
is real semantic work that the synthetic provider could not do — and tier 1
genuinely abstains. But the verdict that follows is `contradiction` because
nothing answered, not because a model judged it so. The policy engine then does
the real work of turning that verdict into REJECT, CONTEST or SUPERSEDE via
authority, evidence and recency.

So: detection is real, resolution is real, **adjudication is not yet**. With a
working Bedrock those three scenarios would be judged rather than failed
closed, and a false-contest rate would become measurable. Until then the tier-2
numbers in any run report mean "escalated and failed closed", and should be
read that way.

## 8. The vector index, measured

`sql/002_indexes.sql` declares a **partial** C-SPANN index:

```sql
CREATE VECTOR INDEX idx_atom_embedding_live
  ON memory_atom (workspace_id, embedding)
  WHERE valid_to IS NULL;
```

Both the prefix column and the partial predicate are load-bearing, and we
learned that the hard way. `tools/bench_vector_index.py` reproduces all of it.

### The index was not being used at all

The first benchmark, at 10,000 atoms in one workspace, found the optimiser
choosing a **full scan plus top-k** — the ANN and brute-force plans came out
byte-identical, 0.98x "speedup", and a meaningless 100% recall because both
sides were doing exact search.

The cause was not scale. It was the query. Narrowing it down:

| query shape | index used |
|---|---|
| `workspace_id` + `valid_to IS NULL` + `status IN (...)` | no — full scan |
| `workspace_id` only | **yes** |
| no filter at all | no — the prefix column needs an equality |
| `workspace_id` + `valid_to`, `status` filtered outside | **yes** |

A vector index can only serve predicates it covers. `status IN (...)` sat
inside the ANN subquery, so CockroachDB could not satisfy it from the index and
fell back to scanning. The fix is in two parts: make the index **partial** on
`valid_to IS NULL` so that predicate is covered, and move the `status` filter
**outside** the ANN subquery, over an over-fetched candidate set (4x k). The
only live rows this discards are `rejected` ones, which are rare.

### After the fix

```
• vector search
  table: memory_atom@idx_atom_embedding_live (partial index)
  target count: 32
  prefix spans: [workspace_id - workspace_id]
```

| | ANN (index) | brute force (forced scan) |
|---|---|---|
| p50 | 202 ms | 510 ms |
| p95 | 345 ms | 749 ms |

**2.5x faster at p50** on 10k atoms, and the gap widens with row count because
brute force is linear.

### Recall, and why the headline number understates it

C-SPANN is approximate, and its search effort is the session variable
`vector_search_beam_size` (CockroachDB default 32). Measured against exact
nearest neighbours:

| beam | recall@8 | 8th-neighbour distance vs exact |
|---|---|---|
| 8 | 30% | 1.0095x |
| 32 (default) | 55% | 1.0042x |
| **64 (ours)** | **77%** | **1.0016x** |

The ID-overlap column looks alarming and the distance column explains why it
should not. The benchmark's synthetic embeddings cluster ~10 claims around each
subject anchor, so neighbours ranked 8 through 32 are nearly equidistant;
swapping one for another changes semantic distance by **0.16%**. For generating
conflict *candidates* that is immaterial — the candidate set is still full of
genuinely near claims.

We run beam 64 (`VECTOR_SEARCH_BEAM_SIZE`, applied as a session option in
`quorum/db/pool.py`). That trades speedup — 2.5x instead of 3.9x at beam 32 —
for recall, deliberately: a contradiction detector that misses a candidate
fails **silently**, which is the worst failure mode this system has.

### What ANN recall does and does not put at risk

Tier-1 structural detection **does not depend on ANN recall at all**. The
neighbourhood query unions an exact `subject_key` lookup served by
`idx_atom_subject_live`, so a same-key contradiction is found even if the
vector index returns nothing useful. That union is not belt-and-braces; it is
what makes the guarantee in §5 hold.

ANN recall is what bounds detection of contradictions that do **not** share a
subject key — the S2 case. There, recall is a real limit and it is stated as
one in §5.

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
