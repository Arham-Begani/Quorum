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
