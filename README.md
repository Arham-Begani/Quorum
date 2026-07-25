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
