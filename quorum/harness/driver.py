"""ONE workload driver, mode-parameterised. [I8]

It constructs the memory client from the factory and knows nothing else about
modes. The same turns, the same seed, the same agents, the same inventory run
against all three. If this file ever needs to ask which mode it is in, the
comparison has stopped being evidence.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid
from dataclasses import dataclass, field

from ..agents.base import build_swarm
from ..db.metrics import metrics
from ..db.pool import crdb_url, make_pool, quorum_dbname
from ..detect.tier2 import Adjudicator
from ..domain.scenarios import catalog
from ..domain.scenarios.base import ActTurn, RememberTurn, ScenarioPlan
from ..embed.bedrock import Embedder
from ..memory.factory import make_memory
from . import anomaly

RUN_SEED = int(os.environ.get("RUN_SEED", 1337))


@dataclass
class TurnRecord:
    kind: str
    agent_id: str
    label: str
    outcome: str
    detail: dict = field(default_factory=dict)


@dataclass
class RunReport:
    run_id: uuid.UUID
    workspace_id: uuid.UUID
    mode: str
    scenario: str
    seed: int
    anomalies: dict
    conflicts: dict
    performance: dict
    turns: list
    providers: dict
    expectation: dict
    duration_s: float
    memory_info: dict

    def to_dict(self) -> dict:
        return {
            "memory": self.memory_info,
            "run_id": str(self.run_id),
            "workspace_id": str(self.workspace_id),
            "mode": self.mode,
            "scenario": self.scenario,
            "seed": self.seed,
            "anomalies": self.anomalies,
            "conflicts": self.conflicts,
            "performance": self.performance,
            "turns": [t.__dict__ for t in self.turns],
            "providers": self.providers,
            "expectation": self.expectation,
            "duration_s": round(self.duration_s, 2),
        }


def run(scenario: str | ScenarioPlan, mode: str, *, seed: int = RUN_SEED,
        pool=None, embedder=None, adjudicator=None, cfg: dict | None = None,
        workspace_id: uuid.UUID | None = None) -> RunReport:
    plan = scenario if isinstance(scenario, ScenarioPlan) else catalog.get(scenario)
    random.seed(seed)

    owns_pool = pool is None
    pool = pool or make_pool(crdb_url(), min_size=4, max_size=10,
                             dbname=quorum_dbname(), app_name=f"quorum-{mode}")
    embedder = embedder or Embedder()
    adjudicator = adjudicator or Adjudicator()
    adjudicator.reset_run()
    metrics.reset()

    run_id = uuid.uuid4()
    workspace_id = workspace_id or uuid.uuid4()
    cfg = dict(cfg or {})
    cfg.update({"run_id": run_id, "adjudicator": adjudicator})
    cfg.setdefault("ann_k", int(os.environ.get("ANN_K", 8)))

    memory = make_memory(mode, pool, embedder, cfg)
    agents = build_swarm(memory, workspace_id)

    with pool.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO run (run_id, mode, scenario, seed, workspace_id) "
                "VALUES (%s,%s,%s,%s,%s)",
                (run_id, mode, plan.id, seed, workspace_id))

    t0 = time.perf_counter()
    turns: list[TurnRecord] = []
    acknowledged = 0

    # Group turns: consecutive RememberTurns sharing a concurrent_group are
    # dispatched simultaneously against a barrier. That is what makes S5 a real
    # race rather than a simulated one.
    for group in _group_turns(plan.turns):
        if len(group) == 1:
            rec, ack = _execute(agents, group[0])
            turns.append(rec)
            acknowledged += ack
        else:
            barrier = threading.Barrier(len(group))
            results: list = [None] * len(group)

            def worker(i: int, turn) -> None:
                barrier.wait()
                results[i] = _execute(agents, turn)

            threads = [threading.Thread(target=worker, args=(i, t), daemon=True)
                       for i, t in enumerate(group)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            for res in results:
                if res is not None:
                    turns.append(res[0])
                    acknowledged += res[1]

    duration = time.perf_counter() - t0

    with pool.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            anomalies = anomaly.detect(cur, workspace_id, run_id,
                                       ground_truth=plan.ground_truth,
                                       acknowledged_writes=acknowledged,
                                       constraints=plan.constraints)

    perf = metrics.snapshot()

    # Latency measured at the remember() boundary, so all three modes are
    # comparable. metrics.durations_ms only sees run_txn, which naive does not
    # use -- reporting that alone would show naive as 0ms and flatter it.
    write_ms = sorted(t.detail.get("latency_ms", 0.0) for t in turns
                      if t.kind == "remember")
    if write_ms:
        pct = lambda p: write_ms[min(len(write_ms) - 1,  # noqa: E731
                                     int(round(p * (len(write_ms) - 1))))]
        perf["p50_write_ms"] = round(pct(0.50), 1)
        perf["p95_write_ms"] = round(pct(0.95), 1)
        perf["p99_write_ms"] = round(pct(0.99), 1)
        perf["writes_measured"] = len(write_ms)

    conflicts = _conflict_summary(anomalies.details.get("conflicts", []))
    expectation = plan.expectations.get(mode)

    report = RunReport(
        run_id=run_id, workspace_id=workspace_id, mode=mode, scenario=plan.id,
        seed=seed, anomalies=anomalies.to_dict(), conflicts=conflicts,
        performance=perf, turns=turns,
        providers={
            "embedder": embedder.info(),
            "tier2": adjudicator.info(),
            "requires_semantic_embeddings": plan.requires_semantic_embeddings,
        },
        expectation=expectation.__dict__ if expectation else {},
        duration_s=duration,
        memory_info=memory.info(),
    )

    with pool.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("UPDATE run SET ended_at = now(), report = %s::JSONB "
                        "WHERE run_id = %s",
                        (json.dumps(report.to_dict(), default=str), run_id))

    if owns_pool:
        pool.close()
    return report
