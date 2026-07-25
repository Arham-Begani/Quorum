"""FastAPI read surface for the dashboard and the demo.

Read-only by design. The API never writes memory: writes happen through the
memory client inside the harness, where the transaction guarantees live. An
HTTP endpoint that could insert an atom would be a second write path, and there
is exactly one. [I3]

    uvicorn quorum.api.server:app --reload --port 8000
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from ..db.pool import crdb_url, make_pool, quorum_dbname
from ..domain.scenarios import catalog
from ..memory.factory import MODES

app = FastAPI(title="Quorum", version="1.0",
              description="Memory consistency layer for multi-agent systems")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"],
)

_pool = None


def pool():
    global _pool
    if _pool is None:
        _pool = make_pool(crdb_url(), min_size=2, max_size=8,
                          dbname=quorum_dbname(), app_name="quorum-api")
    return _pool


def rows(sql: str, params=(), *, aost: str = "") -> list[dict]:
    with pool().connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql.replace("{AOST}", aost), params)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def _json_safe(obj):
    if isinstance(obj, (uuid.UUID, datetime)):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


@app.get("/health")
def health():
    try:
        r = rows("SELECT version() AS v, current_database() AS db")
        return {"ok": True, "cluster": r[0]["v"], "database": r[0]["db"]}
    except Exception as exc:
        raise HTTPException(503, f"database unreachable: {exc}") from exc


@app.get("/scenarios")
def scenarios():
    return [
        {"id": s.id, "title": s.title, "tier": s.tier,
         "description": s.description,
         "wrong_action_note": s.wrong_action_note,
         "requires_semantic_embeddings": s.requires_semantic_embeddings,
         "expectations": {m: e.__dict__ for m, e in s.expectations.items()}}
        for s in catalog.CATALOG.values()
    ]


@app.get("/runs")
def runs(scenario: str | None = None, mode: str | None = None, limit: int = 50):
    sql = ("SELECT run_id, mode, scenario, seed, workspace_id, started_at, ended_at "
           "FROM run WHERE 1=1")
    params: list = []
    if scenario:
        sql += " AND scenario = %s"
        params.append(scenario)
    if mode:
        sql += " AND mode = %s"
        params.append(mode)
    sql += " ORDER BY started_at DESC LIMIT %s"
    params.append(limit)
    return _json_safe(rows(sql, tuple(params)))


@app.get("/runs/{run_id}")
def run_detail(run_id: uuid.UUID):
    r = rows("SELECT run_id, mode, scenario, seed, workspace_id, started_at, "
             "ended_at, report FROM run WHERE run_id = %s", (run_id,))
    if not r:
        raise HTTPException(404, "run not found")
    return _json_safe(r[0])


@app.get("/compare/{scenario}")
def compare(scenario: str):
    """Latest run per mode for one scenario -- the three-mode split screen."""
    out = {}
    for mode in MODES:
        r = rows("SELECT run_id, mode, scenario, workspace_id, report, started_at "
                 "FROM run WHERE scenario = %s AND mode = %s AND report IS NOT NULL "
                 "ORDER BY started_at DESC LIMIT 1", (scenario, mode))
        if r:
            out[mode] = _json_safe(r[0])
    if not out:
        raise HTTPException(404, f"no completed runs for {scenario}")
    plan = catalog.get(scenario)
    return {"scenario": plan.id, "title": plan.title,
            "description": plan.description,
            "wrong_action_note": plan.wrong_action_note, "modes": out}


@app.get("/conflicts")
def conflicts(run_id: uuid.UUID | None = None,
              workspace_id: uuid.UUID | None = None, limit: int = 200):
    sql = ("SELECT id, workspace_id, run_id, incoming_atom_id, existing_atom_id, "
           "subject_key, detector, similarity, verdict, resolution, policy_rule, "
           "rationale, adjudicator_ms, detected_at FROM memory_conflict WHERE 1=1")
    params: list = []
    if run_id:
        sql += " AND run_id = %s"
        params.append(run_id)
    if workspace_id:
        sql += " AND workspace_id = %s"
        params.append(workspace_id)
    sql += " ORDER BY detected_at DESC LIMIT %s"
    params.append(limit)
    return _json_safe(rows(sql, tuple(params)))


@app.get("/atoms")
def atoms(workspace_id: uuid.UUID, live_only: bool = False, limit: int = 500):
    sql = ("SELECT id, workspace_id, subject_key, predicate, object_text, object_json, "
           "writer_agent_id, writer_role, confidence, evidence_count, valid_from, "
           "valid_to, superseded_by, status, visibility FROM memory_atom "
           "WHERE workspace_id = %s")
    if live_only:
        sql += " AND valid_to IS NULL"
    sql += " ORDER BY valid_from LIMIT %s"
    return _json_safe(rows(sql, (workspace_id, limit)))


@app.get("/actions")
def actions(run_id: uuid.UUID | None = None, workspace_id: uuid.UUID | None = None):
    sql = ("SELECT id, workspace_id, run_id, agent_id, action_type, payload, "
           "required_keys, gate_result, justifying_atom_ids, executed, outcome, "
           "created_at FROM action_log WHERE 1=1")
    params: list = []
    if run_id:
        sql += " AND run_id = %s"
        params.append(run_id)
    if workspace_id:
        sql += " AND workspace_id = %s"
        params.append(workspace_id)
    sql += " ORDER BY created_at"
    return _json_safe(rows(sql, tuple(params)))
