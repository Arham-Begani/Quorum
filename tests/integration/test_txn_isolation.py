"""THE FLAGSHIP TEST. This is the thesis, expressed as code.

Two threads write contradictory atoms about the same subject key at the same
instant. After every single race, memory must hold either exactly one active
atom, or a contested pair. It must NEVER hold two active atoms asserting
different values, because that is memory that is internally inconsistent and
will produce a wrong action.

Run it 100x in CI:

    QUORUM_ISOLATION_ITERATIONS=100 pytest tests/integration/test_txn_isolation.py

The companion test at the bottom is what stops this from being vacuous: it
asserts that `naive` DOES produce the forbidden state under identical
conditions. A test that passes because the race never happens proves nothing,
so we prove the race happens.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

from quorum.db.metrics import metrics
from quorum.memory.factory import make_memory
from quorum.memory.schema import Claim, Status

from ..conftest import needs_db

pytestmark = needs_db

ITERATIONS = int(os.environ.get("QUORUM_ISOLATION_ITERATIONS", 25))
RACE_DELAY_MS = int(os.environ.get("QUORUM_ISOLATION_DELAY_MS", 40))
KEY = "trip:1:hotel.checkin_date"


def _contradictory_write(mem, ws, agent_id, role, date, conf=0.7):
    return mem.remember(Claim(ws, KEY, "equals", f"check-in is {date}",
                              {"date": date}, agent_id, role, conf))


def _race(mem, ws) -> list:
    """Two writers, released together by a barrier."""
    barrier = threading.Barrier(2)
    results: list = [None, None]
    spec = [("lodging-1", "lodging_agent", "2026-09-14"),
            ("ground-1", "ground_agent", "2026-09-15")]

    def worker(i):
        agent_id, role, date = spec[i]
        barrier.wait()
        results[i] = _contradictory_write(mem, ws, agent_id, role, date)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def _final_state(pool, ws) -> tuple[list, list]:
    with pool.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, object_json::STRING FROM memory_atom "
                "WHERE workspace_id = %s AND valid_to IS NULL", (ws,))
            live = cur.fetchall()
    active = [r for r in live if r[0] == Status.ACTIVE]
    contested = [r for r in live if r[0] == Status.CONTESTED]
    return active, contested


def _cleanup(pool, ws):
    with pool.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_atom WHERE workspace_id = %s", (ws,))
            cur.execute("DELETE FROM memory_conflict WHERE workspace_id = %s", (ws,))


def test_quorum_never_leaves_two_contradictory_active_atoms(pool, embedder, adjudicator):
    metrics.reset()
    violations = []
    outcomes = {"single_active": 0, "contested_pair": 0}
    total_retries = 0

    for i in range(ITERATIONS):
        ws = uuid.uuid4()
        mem = make_memory("quorum", pool, embedder, {
            "run_id": None, "adjudicator": adjudicator,
            "race_delay_ms": RACE_DELAY_MS,
        })
        results = _race(mem, ws)
        total_retries += sum(r.retries for r in results if r is not None)

        errors = [r.error for r in results if r is not None and r.error]
        assert not errors, f"iteration {i}: write errored: {errors}"

        active, contested = _final_state(pool, ws)
        distinct_active_values = {a[1] for a in active}

        if len(distinct_active_values) > 1:
            violations.append({"iteration": i, "active": active,
                               "contested": contested})
        elif contested:
            outcomes["contested_pair"] += 1
        else:
            outcomes["single_active"] += 1

        _cleanup(pool, ws)

    assert not violations, (
        f"{len(violations)}/{ITERATIONS} races left two contradictory ACTIVE atoms. "
        f"This is the exact failure Quorum exists to prevent. First: {violations[0]}")

    print(f"\n  {ITERATIONS} races: {outcomes}, 40001 retries={total_retries}")
    assert outcomes["single_active"] + outcomes["contested_pair"] == ITERATIONS


def test_the_race_actually_happens_in_naive(pool, embedder):
    """Proves the flagship test is not vacuous.

    If `naive` under identical conditions never produced the forbidden state,
    the test above would be passing because the race never occurs, not because
    serializable isolation prevents it. It does occur, and naive does fail.
    """
    forbidden = 0
    iterations = max(5, ITERATIONS // 5)

    for _ in range(iterations):
        ws = uuid.uuid4()
        mem = make_memory("naive", pool, embedder, {"race_delay_ms": RACE_DELAY_MS})
        _race(mem, ws)
        active, _ = _final_state(pool, ws)
        if len({a[1] for a in active}) > 1:
            forbidden += 1
        _cleanup(pool, ws)

    assert forbidden > 0, (
        "naive never produced two contradictory active atoms, so the race window "
        "is not being exercised and the isolation test above proves nothing. "
        "Increase QUORUM_ISOLATION_DELAY_MS.")
    print(f"\n  naive left contradictory memory in {forbidden}/{iterations} races")


def test_quorum_records_real_retries_under_contention(pool, embedder, adjudicator):
    """40001 is the system working. It must be observed, counted, and bounded."""
    metrics.reset()
    for _ in range(max(5, ITERATIONS // 3)):
        ws = uuid.uuid4()
        mem = make_memory("quorum", pool, embedder, {
            "adjudicator": adjudicator, "race_delay_ms": RACE_DELAY_MS})
        _race(mem, ws)
        _cleanup(pool, ws)

    assert metrics.total_give_ups() == 0, "a write exhausted its retry budget"
    print(f"\n  observed 40001 retries: {metrics.total_retries()}")
