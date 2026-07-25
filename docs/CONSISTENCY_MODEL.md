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
