"""Measure whether the distributed vector index actually does work.

The honest gap in this submission: at demo row counts the planner reads
memory_atom with a full scan and a top-k sort, because with a handful of atoms
per workspace that genuinely is cheaper than descending a C-SPANN index. So the
index exists, is correct, and has never been observed carrying load -- and
"Distributed Vector Indexing" is a required tool.

This closes that. It seeds a realistic workspace, then measures three things:

  1. PLAN     does the optimiser choose idx_atom_embedding at this scale?
  2. LATENCY  ANN search vs the same query forced through a full scan
  3. RECALL   do the ANN results agree with exact nearest neighbours?

Recall matters as much as latency. An index that is fast and wrong would make
the neighbourhood read miss conflict candidates, which is a silent detection
failure -- the worst failure mode this system has. Note that tier-1 detection
does not depend on ANN recall at all, because the neighbourhood query unions an
exact subject_key lookup; ANN is what generalises to contradictions that do NOT
share a key.

    python tools/bench_vector_index.py --atoms 10000
    python tools/bench_vector_index.py --cleanup
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quorum.db.pool import (  # noqa: E402
    DDL_STATEMENT_TIMEOUT_MS, crdb_url, make_pool, quorum_dbname,
)
from quorum.embed.synthetic import embed  # noqa: E402
from quorum.memory.base import vector_literal  # noqa: E402

# A fixed workspace so the benchmark is idempotent and easy to clean up.
BENCH_WS = uuid.UUID("00000000-0000-4000-8000-0000feedbeef")
RESULTS = Path("runs/vector_index_bench.json")

ATTRIBUTES = [
    "hotel.checkin_date", "hotel.checkout_date", "hotel.nightly_rate",
    "flight.arrival_date", "flight.departure_date", "flight.number",
    "ground.transfer_slot", "budget.ceiling_usd",
    "traveller.contact_preference", "traveller.price_flexibility",
]


def claim_text(i: int) -> tuple[str, str]:
    """A subject_key and a claim body, spread over many trips and attributes."""
    attr = ATTRIBUTES[i % len(ATTRIBUTES)]
    trip = i // len(ATTRIBUTES)
    key = f"trip:{trip}:{attr}"
    return key, f"{key} equals value-{i}"


def seed(pool, n: int, batch: int = 250) -> float:
    with pool.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM memory_atom WHERE workspace_id = %s",
                        (BENCH_WS,))
            have = cur.fetchone()[0]
    if have >= n:
        print(f"  already seeded: {have} atoms")
        return 0.0

    todo = n - have
    print(f"  seeding {todo} atoms (have {have}) ...")
    t0 = time.perf_counter()

    # Generate each batch immediately before inserting it and let it go
    # afterwards. Materialising every vector first costs roughly 40 KB per atom
    # in Python objects and string literals, which is how a 10k seed turns into
    # several hundred MB of resident memory on a laptop.
    inserted = 0
    with pool.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for start in range(have, n, batch):
                stop = min(start + batch, n)
                params: list = []
                for i in range(start, stop):
                    key, text = claim_text(i)
                    params += [BENCH_WS, key, text, vector_literal(embed(text))]
                values = ",".join(
                    "(%s,%s,'equals',%s,%s::VECTOR,'bench-1','flight_agent',0.5,1,'active','workspace')"
                    for _ in range(stop - start))
                cur.execute(
                    "INSERT INTO memory_atom (workspace_id, subject_key, predicate, "
                    "object_text, embedding, writer_agent_id, writer_role, confidence, "
                    f"evidence_count, status, visibility) VALUES {values}", params)
                inserted += stop - start
                if inserted % 1000 < batch or stop == n:
                    print(f"    {inserted}/{todo}  ({time.perf_counter() - t0:.0f}s)")
    return time.perf_counter() - t0


# Mirrors MemoryClient._neighbourhood: the ANN subquery filters only on what
# the partial vector index covers, and status is applied outside over an
# over-fetched candidate set.
ANN_SQL = """
SELECT id FROM (
    SELECT id, status FROM memory_atom
    WHERE workspace_id = %s AND valid_to IS NULL
    ORDER BY embedding <-> %s::VECTOR LIMIT %s
) AS ann
WHERE status IN ('active','contested')
"""

# Forcing the primary index makes the optimiser read every row and sort, which
# is exactly the brute-force fallback the fallback plan describes.
BRUTE_SQL = """
SELECT id FROM (
    SELECT id, status FROM memory_atom@memory_atom_pkey
    WHERE workspace_id = %s AND valid_to IS NULL
    ORDER BY embedding <-> %s::VECTOR LIMIT %s
) AS brute
WHERE status IN ('active','contested')
"""


def explain(cur, sql: str, vec: str, k: int) -> str:
    cur.execute("EXPLAIN " + sql, (BENCH_WS, vec, k))
    return "\n".join(str(r[0]) for r in cur.fetchall())


def timed(cur, sql: str, vec: str, k: int, reps: int) -> tuple[list[str], list[float]]:
    ids, times = [], []
    for r in range(reps):
        t0 = time.perf_counter()
        cur.execute(sql, (BENCH_WS, vec, k))
        rows = [str(x[0]) for x in cur.fetchall()]
        times.append((time.perf_counter() - t0) * 1000)
        if r == 0:
            ids = rows
    return ids, times


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--atoms", type=int, default=10000)
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--queries", type=int, default=15)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--cleanup", action="store_true")
    args = ap.parse_args()

    pool = make_pool(crdb_url(), min_size=1, max_size=4, dbname=quorum_dbname(),
                     app_name="quorum-vecbench",
                     statement_timeout_ms=DDL_STATEMENT_TIMEOUT_MS)

    if args.cleanup:
        with pool.connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("DELETE FROM memory_atom WHERE workspace_id = %s", (BENCH_WS,))
        print("  benchmark workspace removed")
        pool.close()
        return 0

    print("=" * 78)
    print("VECTOR INDEX BENCHMARK")
    print("=" * 78)
    seed_s = seed(pool, args.atoms)

    with pool.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("ANALYZE memory_atom")
            cur.execute("SELECT count(*) FROM memory_atom WHERE workspace_id = %s",
                        (BENCH_WS,))
            total = cur.fetchone()[0]
            print(f"\n  atoms in benchmark workspace: {total}")

            probe_key, probe_text = claim_text(3)
            probe = vector_literal(embed(probe_text))

            print("\n" + "-" * 78)
            print("1. PLAN")
            print("-" * 78)
            plan = explain(cur, ANN_SQL, probe, args.k * 4)
            for line in plan.splitlines():
                print("  " + line)
            uses_index = "idx_atom_embedding_live" in plan
            print(f"\n  optimiser chose the vector index: "
                  f"{'YES' if uses_index else 'NO — full scan + top-k'}")

            print("\n" + "-" * 78)
            print("2. LATENCY and 3. RECALL")
            print("-" * 78)
            ann_ms, brute_ms, recalls = [], [], []
            for q in range(args.queries):
                _, text = claim_text(q * 37 + 5)
                vec = vector_literal(embed(text))
                ann_ids, a_t = timed(cur, ANN_SQL, vec, args.k * 4, args.reps)
                bru_ids, b_t = timed(cur, BRUTE_SQL, vec, args.k * 4, args.reps)
                ann_ms += a_t
                brute_ms += b_t
                if bru_ids:
                    recalls.append(len(set(ann_ids) & set(bru_ids)) / len(bru_ids))

            def p(v, q):
                s = sorted(v)
                return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]

            ann_p50, ann_p95 = p(ann_ms, .5), p(ann_ms, .95)
            bru_p50, bru_p95 = p(brute_ms, .5), p(brute_ms, .95)
            recall = statistics.mean(recalls) if recalls else 0.0

            print(f"  ANN (index path)   p50 {ann_p50:7.1f} ms   p95 {ann_p95:7.1f} ms")
            print(f"  brute force (scan) p50 {bru_p50:7.1f} ms   p95 {bru_p95:7.1f} ms")
            speedup = bru_p50 / ann_p50 if ann_p50 else 0
            print(f"  speedup at p50     {speedup:.2f}x")
            print(f"  recall@{args.k} vs exact  {recall * 100:.1f}%")

    payload = {
        "atoms": total, "k": args.k, "queries": args.queries, "reps": args.reps,
        "seed_seconds": round(seed_s, 1),
        "optimiser_uses_vector_index": uses_index,
        "plan": plan,
        "ann_p50_ms": round(ann_p50, 2), "ann_p95_ms": round(ann_p95, 2),
        "brute_p50_ms": round(bru_p50, 2), "brute_p95_ms": round(bru_p95, 2),
        "speedup_p50": round(speedup, 2),
        "recall_at_k": round(recall, 4),
    }
    RESULTS.parent.mkdir(exist_ok=True)
    RESULTS.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nwrote {RESULTS}")
    pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
