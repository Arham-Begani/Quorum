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
