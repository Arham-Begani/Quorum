"""Run the three-mode comparison and emit the report.

    python -m quorum.harness.report --scenario S1_checkin_date
    python -m quorum.harness.report --all
    python -m quorum.harness.report --scenario S5_concurrent_race --delay-ms 50

Writes JSON to runs/ and, if S3_BUCKET and AWS credentials are present, to S3.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ..db.pool import crdb_url, make_pool, quorum_dbname
from ..detect.tier2 import Adjudicator
from ..domain.scenarios import catalog
from ..domain.scenarios.base import check
from ..embed.bedrock import Embedder
from ..memory.factory import MODES
from . import aws_export, driver

RUNS_DIR = Path("runs")


def compare(scenario_id: str, *, modes=MODES, seed: int, delay_ms: int,
            pool=None, embedder=None, adjudicator=None) -> dict:
    plan = catalog.get(scenario_id)
    reports = {}
    for mode in modes:
        rep = driver.run(plan, mode, seed=seed, pool=pool, embedder=embedder,
                         adjudicator=adjudicator,
                         cfg={"race_delay_ms": delay_ms})
        reports[mode] = rep.to_dict()
    return {"scenario": plan.id, "title": plan.title, "tier": plan.tier,
            "description": plan.description,
            "requires_semantic_embeddings": plan.requires_semantic_embeddings,
            "embedder_offline": bool(embedder is not None and not embedder.is_semantic),
            "wrong_action_note": plan.wrong_action_note,
            "seed": seed, "delay_ms": delay_ms, "modes": reports}


def verdicts(result: dict) -> dict:
    """Did each mode do what the scenario says it should?

    A scenario that needs real semantic embeddings, run with the offline
    embedder, is reported as `blocked` rather than `pass` or `fail`. It has not
    been shown to work and it has not been shown to be broken -- the experiment
    could not be performed. Counting it either way would be dishonest.
    """
    plan = catalog.get(result["scenario"])
    offline = result.get("embedder_offline", False)
    unavailable = plan.requires_semantic_embeddings and offline
    out = {}
    for mode, rep in result["modes"].items():
        exp = plan.expectations.get(mode)
        if exp is None:
            continue
        a = rep["anomalies"]
        checks = {
            "contradictory_active_pairs": check(exp.contradictory_active_pairs,
                                                a["contradictory_active_pairs"]),
            "wrong_actions": check(exp.wrong_actions, a["wrong_actions"]),
            "blocked_actions": check(exp.blocked_actions, a["blocked_actions"]),
        }
        passed = all(checks.values())
        # Ask the CLIENT whether it depends on the semantic layer rather than
        # asking which mode it is. Modes without detection have expectations
        # that remain meaningful offline. [I8]
        semantic = rep.get("memory", {}).get("uses_semantic_layer", False)
        blocked = unavailable and semantic and not passed
        out[mode] = {"pass": passed, "blocked": blocked, "checks": checks,
                     "note": exp.note}
    return out


def print_comparison(result: dict) -> None:
    plan = catalog.get(result["scenario"])
    print("\n" + "=" * 82)
    print(f"{plan.id} — {plan.title}   [{plan.tier}]")
    print("=" * 82)
    print(f"  {plan.description}")
    if plan.requires_semantic_embeddings:
        print("\n  NOTE: the conflicting claims do not share a subject_key, so this")
        print("        scenario requires real semantic embeddings (Bedrock Titan).")
    print()
    header = (f"{'mode':<10}{'contradictory':<15}{'wrong':<8}{'blocked':<9}"
              f"{'contested':<11}{'40001':<8}{'p50ms':<8}{'expected?'}")
    print(header)
    print("-" * 82)
    vs = verdicts(result)
    for mode in MODES:
        rep = result["modes"].get(mode)
        if not rep:
            continue
        a, p = rep["anomalies"], rep["performance"]
        v = vs.get(mode, {})
        status = "BLOCKED" if v.get("blocked") else ("PASS" if v.get("pass") else "FAIL")
        print(f"{mode:<10}{a['contradictory_active_pairs']:<15}{a['wrong_actions']:<8}"
              f"{a['blocked_actions']:<9}{a['contested_atoms']:<11}"
              f"{p['txn_retries']:<8}{p['p50_write_ms']:<8}"
              f"{status}")
    print()
    for mode in MODES:
        v = vs.get(mode)
        if not v:
            continue
        if v.get("blocked"):
            print(f"  {mode}: NOT TESTED — needs real semantic embeddings; the offline")
            print(f"            embedder cannot place distinct subject keys near each")
            print(f"            other, so tier 2 never fires. Expected: {v['note']}")
        elif not v["pass"]:
            failed = [k for k, ok in v["checks"].items() if not ok]
            print(f"  {mode}: FAILED on {', '.join(failed)} — expected: {v['note']}")
        else:
            print(f"  {mode}: {v['note']}")

    conflicts = result["modes"].get("quorum", {}).get("conflicts", {})
    if conflicts.get("detected"):
        print(f"\n  quorum detections: {conflicts['detected']} "
              f"(tier1={conflicts['tier1']}, tier2={conflicts['tier2']}) "
              f"resolutions={conflicts['resolutions']} rules={conflicts['policy_rules']}")


def _aws_provenance(payload: dict, out: Path, results: list) -> None:
    """Ship the report to S3 and the counters to CloudWatch, and record the
    outcome of both INSIDE the report.

    Recording it matters: a report that is silent about its exports lets a
    reader assume they happened. The status lands in the artifact so "we export
    to S3 and CloudWatch" is either demonstrably true for this run or
    demonstrably not, with the reason attached.
    """
    metrics = aws_export.export_metrics(results)
    uploaded = aws_export.upload_report(out)   # after metrics, so the S3 copy
                                               # can record the metric outcome
    payload["aws"] = {"s3": uploaded, "cloudwatch": metrics}
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    if uploaded["ok"]:
        # rewrite changed the file; re-upload so S3 holds the final bytes
        uploaded = aws_export.upload_report(out)
        payload["aws"]["s3"] = uploaded

    print("\nAWS export")
    aws_export.print_status("S3        ", uploaded)
    aws_export.print_status("CloudWatch", metrics)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--modes", default=",".join(MODES))
    ap.add_argument("--seed", type=int, default=driver.RUN_SEED)
    ap.add_argument("--delay-ms", type=int, default=0,
                    help="widen the read->write race window; disclosed in the report")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if not args.scenario and not args.all:
        ap.error("pass --scenario <id> or --all")

    scenarios = list(catalog.SCENARIO_IDS) if args.all else [args.scenario]
    modes = tuple(m.strip() for m in args.modes.split(",") if m.strip())

    pool = make_pool(crdb_url(), min_size=4, max_size=10,
                     dbname=quorum_dbname(), app_name="quorum-harness")
    embedder = Embedder()
    adjudicator = Adjudicator()

    print("=" * 82)
    print("QUORUM — three-mode comparison")
    print("=" * 82)
    print(f"  embedder     : {embedder.info()['provider']}")
    print(f"  tier-2       : {adjudicator.info()['provider']}")
    print(f"  seed         : {args.seed}")
    print(f"  race delay   : {args.delay_ms} ms (disclosed)")
    if embedder.is_offline or adjudicator.is_offline:
        print("\n  WARNING: running without Bedrock. The embedder and/or tier-2")
        print("           adjudicator are offline stand-ins; see docs/CONSISTENCY_MODEL.md")
        print("           for exactly what that does and does not prove.")

    results = []
    try:
        for sid in scenarios:
            result = compare(sid, modes=modes, seed=args.seed,
                             delay_ms=args.delay_ms, pool=pool,
                             embedder=embedder, adjudicator=adjudicator)
            result["verdicts"] = verdicts(result)
            print_comparison(result)
            results.append(result)
    finally:
        pool.close()

    RUNS_DIR.mkdir(exist_ok=True)
    out = Path(args.out) if args.out else RUNS_DIR / (
        f"{scenarios[0]}.json" if len(scenarios) == 1 else "all_scenarios.json")
    payload = {"results": results,
               "providers": {"embedder": embedder.info(),
                             "tier2": adjudicator.info()}}
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {out}")
    _aws_provenance(payload, out, results)

    checked = [v for r in results for v in r["verdicts"].values() if not v.get("blocked")]
    blocked = [v for r in results for v in r["verdicts"].values() if v.get("blocked")]
    failed = [v for v in checked if not v["pass"]]
    print(f"\nOVERALL: {len(checked) - len(failed)}/{len(checked)} checks passed", end="")
    if blocked:
        print(f", {len(blocked)} not testable without Bedrock", end="")
    print(f"\n         {'PASS' if not failed else 'FAIL'}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
