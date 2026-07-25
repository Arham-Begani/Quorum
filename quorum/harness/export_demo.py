"""Export a seeded, read-only demo snapshot for the dashboard.

The hackathon rules require a functional demo URL that works without
credentials. A judge who never runs a workload must still see the whole story
on first load, so the dashboard ships with a baked snapshot of a completed
three-mode comparison and only reaches for the live API if one is configured.

    python -m quorum.harness.export_demo            # uses the latest runs
    python -m quorum.harness.export_demo --rerun    # run all scenarios first
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ..db.pool import crdb_url, make_pool, quorum_dbname
from ..detect.tier2 import Adjudicator
from ..domain.scenarios import catalog
from ..embed.bedrock import Embedder
from ..memory.factory import MODES
from . import report as report_mod

OUT = Path("dashboard/public/demo-snapshot.json")


def _rows(cur, sql, params=()):
    cur.execute(sql, params)
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _safe(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe(v) for v in obj]
    if hasattr(obj, "hex") and hasattr(obj, "int"):     # uuid.UUID
        return str(obj)
    return obj


def build(pool, embedder, adjudicator) -> dict:
    scenarios = []
    with pool.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for sid in catalog.SCENARIO_IDS:
                plan = catalog.get(sid)
                modes = {}
                for mode in MODES:
                    latest = _rows(cur,
                        "SELECT run_id, workspace_id, report, started_at FROM run "
                        "WHERE scenario=%s AND mode=%s AND report IS NOT NULL "
                        "ORDER BY started_at DESC LIMIT 1", (sid, mode))
                    if not latest:
                        continue
                    row = latest[0]
                    rep = row["report"]
                    rep = rep if isinstance(rep, dict) else json.loads(rep)
                    ws, rid = row["workspace_id"], row["run_id"]
                    modes[mode] = {
                        "run_id": str(rid),
                        "workspace_id": str(ws),
                        "started_at": _safe(row["started_at"]),
                        "report": rep,
                        "atoms": _safe(_rows(cur,
                            "SELECT id, subject_key, predicate, object_text, object_json, "
                            "writer_agent_id, writer_role, confidence, evidence_count, "
                            "valid_from, valid_to, superseded_by, status FROM memory_atom "
                            "WHERE workspace_id=%s ORDER BY valid_from", (ws,))),
                        "conflicts": _safe(_rows(cur,
                            "SELECT incoming_atom_id, existing_atom_id, subject_key, "
                            "detector, similarity, verdict, resolution, policy_rule, "
                            "rationale, adjudicator_ms, detected_at FROM memory_conflict "
                            "WHERE run_id=%s ORDER BY detected_at", (rid,))),
                        "actions": _safe(_rows(cur,
                            "SELECT agent_id, action_type, payload, required_keys, "
                            "gate_result, justifying_atom_ids, executed, outcome, "
                            "created_at FROM action_log WHERE run_id=%s "
                            "ORDER BY created_at", (rid,))),
                    }
                if not modes:
                    continue
                result = {"scenario": sid,
                          "embedder_offline": embedder.is_offline,
                          "modes": {m: v["report"] for m, v in modes.items()}}
                scenarios.append({
                    "id": plan.id, "title": plan.title, "tier": plan.tier,
                    "description": plan.description,
                    "wrong_action_note": plan.wrong_action_note,
                    "requires_semantic_embeddings": plan.requires_semantic_embeddings,
                    "expectations": {m: e.__dict__ for m, e in plan.expectations.items()},
                    "verdicts": report_mod.verdicts(result),
                    "modes": modes,
                })

            cluster = _rows(cur, "SELECT version() AS v")[0]["v"]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cluster": cluster,
        "providers": {"embedder": embedder.info(), "tier2": adjudicator.info()},
        "modes": list(MODES),
        "scenarios": scenarios,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rerun", action="store_true",
                    help="run all scenarios in all modes before exporting")
    ap.add_argument("--delay-ms", type=int, default=40)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    pool = make_pool(crdb_url(), min_size=2, max_size=10,
                     dbname=quorum_dbname(), app_name="quorum-export")
    embedder, adjudicator = Embedder(), Adjudicator()
    try:
        if args.rerun:
            for sid in catalog.SCENARIO_IDS:
                print(f"  running {sid} ...")
                report_mod.compare(sid, seed=1337, delay_ms=args.delay_ms,
                                   pool=pool, embedder=embedder,
                                   adjudicator=adjudicator)
        snapshot = build(pool, embedder, adjudicator)
    finally:
        pool.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    size = out.stat().st_size / 1024
    print(f"wrote {out} ({size:.0f} KB, {len(snapshot['scenarios'])} scenarios)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
