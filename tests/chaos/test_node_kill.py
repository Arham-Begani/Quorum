"""Chaos: kill a node mid-workload and assert memory stays consistent.

Requires a LOCAL multi-node CockroachDB cluster, because you cannot kill a node
in CockroachDB Cloud Basic -- there are no nodes to reach. Bring one up with:

    bash infra/chaos/start_cluster.sh          # 3 nodes on 26301-26303
    CRDB_URL=postgresql://root@localhost:26301/defaultdb?sslmode=disable \\
    QUORUM_CHAOS=1 pytest tests/chaos -q -s
    bash infra/chaos/stop_cluster.sh

Skipped by default. It is opt-in rather than automatic because a test that
silently does nothing when its dependency is missing is worse than one that
says so.

What it asserts: while a node is down, writes CONTINUE, the retry counter rises
and settles, and contradictory_active_pairs stays 0. Availability without
consistency would be easy; the claim is both.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid

import pytest

from quorum.db.metrics import metrics
from quorum.memory.factory import make_memory
from quorum.memory.schema import Claim, Status

CHAOS_ENABLED = os.environ.get("QUORUM_CHAOS") == "1"
KILL_CONTAINER = os.environ.get("QUORUM_CHAOS_CONTAINER", "quorum-crdb-3")
KEY = "trip:1:hotel.checkin_date"

pytestmark = pytest.mark.skipif(
    not CHAOS_ENABLED,
    reason="set QUORUM_CHAOS=1 and point CRDB_URL at a local multi-node cluster "
           "(see infra/chaos/start_cluster.sh)")


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def test_writes_continue_and_no_anomaly_leaks_while_a_node_is_down(pool, embedder,
                                                                   adjudicator):
    metrics.reset()
    stop = threading.Event()
    written, errors = [], []
    ws = uuid.uuid4()
    mem = make_memory("quorum", pool, embedder,
                      {"adjudicator": adjudicator, "txn_max_retries": 12})

    def writer(worker: int) -> None:
        i = 0
        while not stop.is_set():
            i += 1
            date = f"2026-09-{14 + (i + worker) % 2:02d}"
            res = mem.remember(Claim(ws, KEY, "equals", f"check-in is {date}",
                                     {"date": date}, f"ground-{worker + 1}",
                                     "ground_agent", 0.7))
            (errors if res.error else written).append(res)
            time.sleep(0.05)

    threads = [threading.Thread(target=writer, args=(w,), daemon=True)
               for w in range(2)]
    for t in threads:
        t.start()

    time.sleep(3)
    before = len(written)

    kill = _docker("stop", KILL_CONTAINER)
    assert kill.returncode == 0, f"could not stop {KILL_CONTAINER}: {kill.stderr}"
    print(f"\n  stopped {KILL_CONTAINER}; writes so far: {before}")

    time.sleep(6)
    during = len(written)

    restart = _docker("start", KILL_CONTAINER)
    assert restart.returncode == 0, f"could not restart: {restart.stderr}"
    time.sleep(5)

    stop.set()
    for t in threads:
        t.join(timeout=10)

    print(f"  writes: {before} before, {during - before} while a node was down, "
          f"{len(written) - during} after recovery")
    print(f"  40001 retries: {metrics.total_retries()}, "
          f"give-ups: {metrics.total_give_ups()}, errors: {len(errors)}")

    assert during > before, (
        "no write completed while the node was down -- the cluster lost "
        "availability, not just a replica")

    with pool.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) FROM memory_atom a, memory_atom b
                   WHERE a.workspace_id = %s AND b.workspace_id = a.workspace_id
                     AND a.subject_key = b.subject_key AND a.id < b.id
                     AND a.valid_to IS NULL AND b.valid_to IS NULL
                     AND a.status = 'active' AND b.status = 'active'
                     AND a.object_json::STRING IS DISTINCT FROM b.object_json::STRING""",
                (ws,))
            contradictory = cur.fetchone()[0]
            cur.execute("DELETE FROM memory_atom WHERE workspace_id = %s", (ws,))
            cur.execute("DELETE FROM memory_conflict WHERE workspace_id = %s", (ws,))

    assert contradictory == 0, (
        f"{contradictory} contradictory active pairs leaked during the node kill. "
        "Availability was preserved but consistency was not, which is the worse "
        "of the two failures.")
    assert metrics.total_give_ups() == 0, "a write exhausted its retry budget"
