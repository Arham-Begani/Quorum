"""M2 PROOF SPIKE — does semantic contradiction detection require serializable isolation?

Two agents write contradictory facts about the same subject key at the same
instant. We run that race 200 times in each of three memory modes and count how
often the database is left holding two mutually contradictory "currently true"
facts.

    naive     detection YES, transaction NO   -- read neighbourhood, then INSERT,
                                                 as two separate autocommitted ops
    txn_only  detection NO,  transaction YES  -- one SERIALIZABLE txn, plain INSERT
    quorum    detection YES, transaction YES  -- neighbourhood read AND write in
                                                 the SAME serializable transaction

That 2x2 is the whole argument. If `naive` fails, isolation is *necessary*. If
`txn_only` also fails, isolation is *not sufficient*. Only the combination is
sound, and only CockroachDB can give you the combination, because the
neighbourhood read is an ANN vector search that has to be in the same
transactional domain as the rows (CLAUDE.md §1).

Deliberate, disclosed methodology note
--------------------------------------
`--delay-ms` inserts a sleep between the neighbourhood read and the write, in
the SAME logical position in all three modes, to widen a race window that is
otherwise sub-millisecond and therefore hard to observe. Widening a real window
to make it measurable is legitimate; inventing one is not. The window exists at
any delay -- run with `--delay-ms 0` to see the unwidened rate. The delay is
printed in the output table and recorded in results.json.

Scope: spike only. No agents, no LLM, no Bedrock, no policy engine, no MemoryClient
ABC. Embeddings are the deterministic synthetic ones from spikes/fake_embed.py.

    python spikes/bootstrap.py            # first, once
    python spikes/prove_race.py --iterations 200 --delay-ms 50
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quorum.db.metrics import metrics  # noqa: E402
from quorum.db.pool import crdb_url, explain_connect_failure, make_pool  # noqa: E402
from quorum.db.txn import run_txn  # noqa: E402
from spikes.fake_embed import embed, to_pg_vector  # noqa: E402

RESULTS_PATH = Path(__file__).with_name("results.json")
BOOTSTRAP_REPORT = Path(__file__).with_name("bootstrap_report.json")

SUBJECT_KEY = "trip:1:hotel.checkin_date"
PREDICATE = "equals"

# The two contradictory claims. Same subject key, both asserting `equals`,
# different scalar values -> a textbook tier-1 structural contradiction.
CLAIM_A = {"role": "lodging_agent", "json": {"date": "2026-09-14"}, "text": "check-in is 2026-09-14"}
CLAIM_B = {"role": "booking_agent", "json": {"date": "2026-09-15"}, "text": "check-in is 2026-09-15"}


# --------------------------------------------------------------------------
# shared plumbing
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class WriteCtx:
    workspace_id: uuid.UUID
    subject_key: str
    predicate: str
    object_text: str
    object_json: dict
    vector_literal: str
    writer_role: str
    confidence: float
    delay_ms: int
    ann_k: int
    distance_op: str


@dataclass
class WriteOutcome:
    mode: str
    resolution: str          # inserted | superseded_then_inserted | error
    retries: int = 0
    latency_ms: float = 0.0
    error: str | None = None


def _sleep_race_window(ctx: WriteCtx) -> None:
    """The disclosed, artificially widened read->write window."""
    if ctx.delay_ms:
        time.sleep(ctx.delay_ms / 1000.0)


def _neighbourhood(cur, ctx: WriteCtx) -> list[dict]:
    """ANN neighbourhood UNION exact subject_key lookup.

    The exact-key branch is not optional. ANN recall is approximate; a
    structural match must never be missed because the vector index happened to
    drop it from the top-k (BUILD.md §4.6). The ANN branch is what generalises
    to claims that contradict without sharing a key -- that is why the vector
    index has to live in the same transactional domain as the rows.
    """
    # Each UNION branch is parenthesised: without them the parser binds the
    # ORDER BY / LIMIT to the whole UNION rather than to the ANN branch, and
    # CockroachDB rejects it outright.
    sql = f"""
        SELECT id, subject_key, predicate, object_json FROM (
            (SELECT id, subject_key, predicate, object_json
             FROM memory_atom
             WHERE workspace_id = %(ws)s AND valid_to IS NULL
               AND status IN ('active','contested')
             ORDER BY embedding {ctx.distance_op} %(vec)s::VECTOR
             LIMIT %(k)s)
          UNION ALL
            (SELECT id, subject_key, predicate, object_json
             FROM memory_atom
             WHERE workspace_id = %(ws)s AND valid_to IS NULL
               AND status IN ('active','contested')
               AND subject_key = %(sk)s)
        ) AS neighbourhood
    """
    cur.execute(sql, {"ws": ctx.workspace_id, "vec": ctx.vector_literal,
                      "k": ctx.ann_k, "sk": ctx.subject_key})
    seen: dict[uuid.UUID, dict] = {}
    for row_id, subject_key, predicate, object_json in cur.fetchall():
        seen[row_id] = {"id": row_id, "subject_key": subject_key,
                        "predicate": predicate, "object_json": object_json}
    return list(seen.values())


def _contradicts(ctx: WriteCtx, existing: dict) -> bool:
    """Tier-1 structural comparison. Pure, deterministic, no network, no LLM.

    Same normalized subject key, both sides assert `equals`, both carry a
    parseable object_json, values differ -> they cannot both be true.
    """
    if existing["subject_key"] != ctx.subject_key:
        return False
    if existing["predicate"] != PREDICATE or ctx.predicate != PREDICATE:
        return False
    if existing["object_json"] is None or ctx.object_json is None:
        return False
    return existing["object_json"] != ctx.object_json


INSERT_SQL = """
INSERT INTO memory_atom
  (id, workspace_id, subject_key, predicate, object_text, object_json,
   embedding, writer_role, confidence, status)
VALUES
  (%(id)s, %(ws)s, %(sk)s, %(pred)s, %(text)s, %(json)s::JSONB,
   %(vec)s::VECTOR, %(role)s, %(conf)s, 'active')
"""

SUPERSEDE_SQL = """
UPDATE memory_atom
   SET valid_to = now(), superseded_by = %(new_id)s, status = 'superseded'
 WHERE id = ANY(%(ids)s::UUID[]) AND valid_to IS NULL
"""


def _resolve_and_write(cur, ctx: WriteCtx, neighbours: list[dict]) -> str:
    """Decide and write. IDENTICAL logic in `naive` and `quorum`.

    This is the point of the experiment: naive and quorum run the same lines of
    code. The ONLY difference is whether the neighbourhood read above and these
    statements are inside one serializable transaction. Nothing else varies.
    """
    new_id = uuid.uuid4()
    conflicting = [n["id"] for n in neighbours if _contradicts(ctx, n)]

    if conflicting:
        # Append-only: the old atom is never deleted or rewritten, only closed
        # out with valid_to / superseded_by / status. [I4]
        cur.execute(SUPERSEDE_SQL, {"new_id": new_id, "ids": conflicting})

    cur.execute(INSERT_SQL, {
        "id": new_id, "ws": ctx.workspace_id, "sk": ctx.subject_key,
        "pred": ctx.predicate, "text": ctx.object_text,
        "json": json.dumps(ctx.object_json), "vec": ctx.vector_literal,
        "role": ctx.writer_role, "conf": ctx.confidence,
    })
    return "superseded_then_inserted" if conflicting else "inserted"


# --------------------------------------------------------------------------
# the three write paths — one signature
# --------------------------------------------------------------------------

def naive_write(pool, ctx: WriteCtx) -> WriteOutcome:
    """Autocommit. Read the neighbourhood, then write — two separate operations.

    This is the honest baseline, not a strawman (CLAUDE.md §15.6). It runs the
    full contradiction check. Its vector index and its rows are even perfectly
    in sync, because here they are the same database — a real deployment with a
    separate vector store (Pinecone + Postgres) would be strictly worse, since
    it also has cross-store replication lag. We are giving naive the best case
    it could possibly have and it still loses, purely for want of a transaction.
    """
    t0 = time.perf_counter()
    try:
        with pool.connection() as conn:
            conn.autocommit = True          # every statement its own implicit txn
            with conn.cursor() as cur:
                neighbours = _neighbourhood(cur, ctx)
                _sleep_race_window(ctx)
                resolution = _resolve_and_write(cur, ctx, neighbours)
        return WriteOutcome("naive", resolution, 0, (time.perf_counter() - t0) * 1000)
    except Exception as exc:  # counted and reported, never swallowed
        return WriteOutcome("naive", "error", 0, (time.perf_counter() - t0) * 1000, repr(exc))


def txn_only_write(pool, ctx: WriteCtx) -> WriteOutcome:
    """One SERIALIZABLE transaction via run_txn. Plain INSERT. No check at all.

    CockroachDB used exactly as designed: no lost updates, no dirty reads, no
    write skew. And no idea that the row it is inserting contradicts one already
    there, because the contradiction lives across two structurally unrelated
    rows and no isolation level has an opinion about semantics.
    """
    t0 = time.perf_counter()

    def body(cur):
        _sleep_race_window(ctx)             # same delay, same position, no read
        cur.execute(INSERT_SQL, {
            "id": uuid.uuid4(), "ws": ctx.workspace_id, "sk": ctx.subject_key,
            "pred": ctx.predicate, "text": ctx.object_text,
            "json": json.dumps(ctx.object_json), "vec": ctx.vector_literal,
            "role": ctx.writer_role, "conf": ctx.confidence,
        })
        return "inserted"

    try:
        out = run_txn(pool, body, label="txn_only")
        return WriteOutcome("txn_only", out.value, out.retries, (time.perf_counter() - t0) * 1000)
    except Exception as exc:
        return WriteOutcome("txn_only", "error", 0, (time.perf_counter() - t0) * 1000, repr(exc))


def quorum_write(pool, ctx: WriteCtx) -> WriteOutcome:
    """Neighbourhood read AND write inside ONE serializable transaction. [I2]

    Byte for byte the same detection and resolution code as `naive`. The only
    change is that it is wrapped in run_txn. If a concurrent writer landed in
    the neighbourhood after our read, CockroachDB refuses the commit with 40001,
    run_txn retries, the retry re-reads and now sees the other fact. There is no
    window in which two contradictory facts both slip through.
    """
    t0 = time.perf_counter()

    def body(cur):
        neighbours = _neighbourhood(cur, ctx)
        _sleep_race_window(ctx)
        return _resolve_and_write(cur, ctx, neighbours)

    try:
        out = run_txn(pool, body, label="quorum")
        return WriteOutcome("quorum", out.value, out.retries, (time.perf_counter() - t0) * 1000)
    except Exception as exc:
        return WriteOutcome("quorum", "error", 0, (time.perf_counter() - t0) * 1000, repr(exc))


# Spike-local dispatch. In the product this is quorum/memory/factory.py and it is
# the only place allowed to branch on mode (I8).
WRITERS = {"naive": naive_write, "txn_only": txn_only_write, "quorum": quorum_write}


# --------------------------------------------------------------------------
# the experiment
# --------------------------------------------------------------------------

CONTRADICTORY_PAIRS_SQL = """
SELECT count(*)
FROM memory_atom a, memory_atom b
WHERE a.workspace_id = %(ws)s
  AND b.workspace_id = a.workspace_id
  AND a.subject_key  = b.subject_key
  AND a.id < b.id
  AND a.valid_to IS NULL AND b.valid_to IS NULL
  AND a.status = 'active' AND b.status = 'active'
  -- CockroachDB canonicalises JSONB, so the text form is a stable equality test
  AND a.object_json::STRING IS DISTINCT FROM b.object_json::STRING
"""


@dataclass
class ModeResult:
    mode: str
    iterations: int = 0
    total_pairs: int = 0
    iterations_with_contradiction: int = 0
    retries_40001: int = 0
    give_ups: int = 0
    errors: int = 0
    error_samples: list[str] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)
    resolutions: dict = field(default_factory=dict)
    active_atom_histogram: dict = field(default_factory=dict)

    @property
    def rate(self) -> float:
        return (self.iterations_with_contradiction / self.iterations * 100) if self.iterations else 0.0

    def pct(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        s = sorted(self.latencies_ms)
        idx = min(len(s) - 1, int(round(p * (len(s) - 1))))
        return s[idx]


def run_iteration(pool, mode: str, workspace_id: uuid.UUID, delay_ms: int,
                  ann_k: int, distance_op: str) -> tuple[list[WriteOutcome], int, int]:
    """One race: two threads, released simultaneously by a barrier."""
    writer = WRITERS[mode]
    contexts = []
    for claim in (CLAIM_A, CLAIM_B):
        vec = to_pg_vector(embed(SUBJECT_KEY, claim["text"]))
        contexts.append(WriteCtx(
            workspace_id=workspace_id, subject_key=SUBJECT_KEY, predicate=PREDICATE,
            object_text=claim["text"], object_json=claim["json"], vector_literal=vec,
            writer_role=claim["role"], confidence=0.8, delay_ms=delay_ms,
            ann_k=ann_k, distance_op=distance_op,
        ))

    barrier = threading.Barrier(len(contexts))
    outcomes: list[WriteOutcome | None] = [None] * len(contexts)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            outcomes[i] = writer(pool, contexts[i])
        except Exception as exc:  # belt and braces; writers already catch
            outcomes[i] = WriteOutcome(mode, "error", 0, 0.0, repr(exc))

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(len(contexts))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with pool.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(CONTRADICTORY_PAIRS_SQL, {"ws": workspace_id})
            pairs = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM memory_atom WHERE workspace_id = %s "
                "AND valid_to IS NULL AND status = 'active'", (workspace_id,))
            active = cur.fetchone()[0]
            # harness cleanup between iterations -- not the memory write path,
            # where there is no DELETE, ever [I4]
            cur.execute("DELETE FROM memory_atom WHERE workspace_id = %s", (workspace_id,))

    return [o for o in outcomes if o is not None], pairs, active


def run_mode(pool, mode: str, iterations: int, delay_ms: int, ann_k: int,
             distance_op: str) -> ModeResult:
    res = ModeResult(mode=mode)
    metrics.reset()
    print(f"\n--- {mode} ---")
    t0 = time.perf_counter()

    for i in range(1, iterations + 1):
        ws = uuid.uuid4()
        outcomes, pairs, active = run_iteration(pool, mode, ws, delay_ms, ann_k, distance_op)

        res.iterations += 1
        res.total_pairs += pairs
        if pairs:
            res.iterations_with_contradiction += 1
        res.active_atom_histogram[active] = res.active_atom_histogram.get(active, 0) + 1
        for o in outcomes:
            res.latencies_ms.append(o.latency_ms)
            res.resolutions[o.resolution] = res.resolutions.get(o.resolution, 0) + 1
            if o.error:
                res.errors += 1
                if len(res.error_samples) < 5:
                    res.error_samples.append(o.error)

        if i % 25 == 0 or i == iterations:
            print(f"  {i:>4}/{iterations}  contradictory iterations={res.iterations_with_contradiction:>4}"
                  f"  40001 retries={metrics.total_retries():>4}"
                  f"  elapsed={time.perf_counter() - t0:>6.1f}s")

    res.retries_40001 = metrics.total_retries()
    res.give_ups = metrics.total_give_ups()
    return res


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def print_table(results: dict[str, ModeResult], delay_ms: int) -> None:
    print("\n" + "=" * 78)
    print(f"RESULTS  (read->write window artificially widened by --delay-ms {delay_ms}, "
          f"applied identically in all three modes)")
    print("=" * 78)
    header = f"{'mode':<11}{'iterations':<12}{'contradictory_pairs':<21}{'rate':<9}{'40001_retries':<15}{'p50_ms'}"
    print(header)
    for mode in ("naive", "txn_only", "quorum"):
        r = results.get(mode)
        if r is None:
            continue
        print(f"{r.mode:<11}{r.iterations:<12}{r.iterations_with_contradiction:<21}"
              f"{r.rate:>5.1f}%   {r.retries_40001:<15}{r.pct(0.50):.1f}")

    print()
    for mode in ("naive", "txn_only", "quorum"):
        r = results.get(mode)
        if r is None:
            continue
        hist = ", ".join(f"{k} active atom(s) x{v}" for k, v in sorted(r.active_atom_histogram.items()))
        print(f"  {r.mode:<9} p95={r.pct(0.95):>7.1f}ms  give_ups={r.give_ups}  errors={r.errors}"
              f"  resolutions={r.resolutions}")
        print(f"  {'':<9} final state: {hist}")
        for sample in r.error_samples:
            print(f"  {'':<9} ERROR SAMPLE: {sample}")


def evaluate_gate(results: dict[str, ModeResult]) -> dict:
    naive = results.get("naive")
    txn_only = results.get("txn_only")
    quorum = results.get("quorum")
    return {
        "naive_shows_contradictions": bool(naive and naive.iterations_with_contradiction > 0),
        "txn_only_shows_contradictions": bool(txn_only and txn_only.iterations_with_contradiction > 0),
        "quorum_zero_contradictions": bool(quorum and quorum.iterations_with_contradiction == 0
                                           and quorum.iterations > 0),
        "quorum_real_40001_retries": bool(quorum and quorum.retries_40001 > 0),
        "no_unexplained_errors": all(r.errors == 0 for r in results.values()),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="M2 proof spike: two-writer race in three modes")
    ap.add_argument("--iterations", type=int, default=200)
    ap.add_argument("--delay-ms", type=int, default=50,
                    help="artificial read->write window, applied identically in all modes")
    ap.add_argument("--ann-k", type=int, default=8)
    ap.add_argument("--modes", default="naive,txn_only,quorum")
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--out", default=str(RESULTS_PATH), help="where to write the JSON report")
    args = ap.parse_args()
    out_path = Path(args.out)

    random.seed(args.seed)

    distance_op = "<->"
    vector_index_syntax = None
    if BOOTSTRAP_REPORT.exists():
        rep = json.loads(BOOTSTRAP_REPORT.read_text(encoding="utf-8"))
        distance_op = rep.get("distance_operator") or distance_op
        vector_index_syntax = rep.get("vector_index_syntax")
    else:
        print("WARNING: spikes/bootstrap_report.json not found — run spikes/bootstrap.py first.\n"
              f"         Falling back to distance operator {distance_op}.")

    try:
        pool = make_pool(crdb_url(), min_size=4, max_size=8, app_name="quorum-spike-race")
    except SystemExit:
        raise
    except Exception as exc:
        print("FAILED to connect:\n  " + explain_connect_failure(exc))
        return 2

    with pool.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]

    print("=" * 78)
    print("QUORUM M2 PROOF SPIKE")
    print("=" * 78)
    print(f"  cluster            : {version}")
    print(f"  vector index       : {vector_index_syntax or 'unknown (bootstrap not run)'}")
    print(f"  distance operator  : {distance_op}")
    print(f"  iterations/mode    : {args.iterations}")
    print(f"  writers/iteration  : 2 (released together by a barrier)")
    print(f"  race window widened: {args.delay_ms} ms between neighbourhood read and write,")
    print(f"                       applied at the same point in all three modes (disclosed)")
    print(f"  subject_key        : {SUBJECT_KEY}")
    print(f"  claim A            : {CLAIM_A['json']}  ({CLAIM_A['role']})")
    print(f"  claim B            : {CLAIM_B['json']}  ({CLAIM_B['role']})")
    print(f"  embeddings         : deterministic synthetic (spikes/fake_embed.py), no Bedrock")

    results: dict[str, ModeResult] = {}
    try:
        for mode in [m.strip() for m in args.modes.split(",") if m.strip()]:
            if mode not in WRITERS:
                print(f"unknown mode {mode!r}")
                return 2
            results[mode] = run_mode(pool, mode, args.iterations, args.delay_ms,
                                     args.ann_k, distance_op)
    finally:
        pool.close()

    print_table(results, args.delay_ms)
    gate = evaluate_gate(results)

    print("\n" + "=" * 78)
    print("M2 GATE (BUILD.md §2.4)")
    print("=" * 78)
    labels = {
        "naive_shows_contradictions": "naive produces two active contradictory atoms at a measurable rate",
        "txn_only_shows_contradictions": "txn_only ALSO produces them (isolation alone is insufficient)",
        "quorum_zero_contradictions": f"quorum produces zero across all {args.iterations} iterations",
        "quorum_real_40001_retries": "real, non-zero 40001 retries observed in quorum",
        "no_unexplained_errors": "no unexplained write errors in any mode",
    }
    for key, label in labels.items():
        print(f"  [{'PASS' if gate[key] else 'FAIL'}]  {label}")

    if not gate["txn_only_shows_contradictions"]:
        print("\n" + "!" * 78)
        print("!! txn_only showed ZERO contradictory pairs.")
        print("!! That contradicts the central claim of this project. Do not build further")
        print("!! until this is understood — see BUILD.md §14.")
        print("!" * 78)

    payload = {
        "spike": "M2_prove_race",
        "cluster_version": version,
        "vector_index_syntax": vector_index_syntax,
        "distance_operator": distance_op,
        "config": {
            "iterations": args.iterations,
            "delay_ms": args.delay_ms,
            "delay_note": ("artificial read->write window, applied identically in all three "
                           "modes, disclosed per BUILD.md §2.4"),
            "ann_k": args.ann_k,
            "writers_per_iteration": 2,
            "seed": args.seed,
            "subject_key": SUBJECT_KEY,
            "claim_a": CLAIM_A,
            "claim_b": CLAIM_B,
            "embedder": "spikes/fake_embed.py (deterministic synthetic, spike-only)",
        },
        "modes": {
            m: {
                "iterations": r.iterations,
                "contradictory_pairs": r.iterations_with_contradiction,
                "total_contradictory_pairs_counted": r.total_pairs,
                "rate_pct": round(r.rate, 2),
                "retries_40001": r.retries_40001,
                "give_ups": r.give_ups,
                "errors": r.errors,
                "error_samples": r.error_samples,
                "p50_ms": round(r.pct(0.50), 2),
                "p95_ms": round(r.pct(0.95), 2),
                "resolutions": r.resolutions,
                "final_active_atom_histogram": {str(k): v for k, v in sorted(r.active_atom_histogram.items())},
            }
            for m, r in results.items()
        },
        "gate": gate,
        "gate_passed": all(gate.values()),
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path}")

    return 0 if all(gate.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
