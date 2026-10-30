"""SAFL 2026-10-30: synthetic before/after demonstration, no external effects.

Runs production memory/gate/policy functions with a recording store and an
offline adjudicator. Does not validate CockroachDB or real model performance.
"""
import argparse
import json
import os
import socket
import sys
import uuid
from pathlib import Path
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    os.chdir(root)
    for key in list(os.environ):
        if key.startswith(("AWS_", "BEDROCK_", "CRDB_", "GCP_", "GOOGLE_")):
            os.environ.pop(key, None)
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    def deny_network(*args, **kwargs):
        raise AssertionError("Network forbidden during synthetic SAFL demonstration")
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.create_connection = deny_network
    from quorum.memory.schema import Action, AgentCtx
    from quorum.memory.quorum import PreparedDecision
    from tests.safl_fixtures import WS, KEY, PRIVATE_TEXT, atom, claim, memory, remember_with_neighbours

    cases = {}
    old = atom()
    incoming = claim()
    mem, store = memory([old])
    result = remember_with_neighbours(mem, incoming, [old])
    gate = mem.act(Action(WS, "booking-1", "synthetic_booking", {}, (incoming.subject_key,)))
    cases["offline_adjudication"] = {
        "resolution":result.resolution, "policy_rule":result.policy_rule,
        "statuses":sorted(a.status for a in store.atoms.values()),
        "dependent_action":gate.gate_result, "executed":gate.executed,
        "audit_reasons":[c["rationale"] for c in store.conflicts],
    }
    mem, store = memory([old])
    with patch.object(mem, "_neighbourhood", return_value=[old]):
        plan = mem._commit(store, incoming, PreparedDecision("[1]"), uuid.UUID(int=999), {},
                           force_contest=(old.id,))
    cases["explicit_force_contest"] = {
        "resolution":plan.resolution, "policy_rule":plan.policy_rule,
        "statuses":sorted(a.status for a in store.atoms.values()),
    }
    for name, visibility, actor in [
            ("other_agent_private", "private", "lodging-1"),
            ("owner_private_control", "private", "research-1"),
            ("shared_control", "workspace", "lodging-1")]:
        private = atom(visibility=visibility)
        mem, store = memory([private])
        recalled = mem.recall("", agent=AgentCtx(actor, "lodging_agent", 3, ()),
                              workspace_id=WS, subject_keys=[KEY])
        gate = mem.act(Action(WS, actor, "synthetic_booking", {}, (KEY,)))
        cases[name] = {"recall_visible_count":len(recalled),
            "action_gate":gate.gate_result, "executed":gate.executed,
            "fixture_value_in_action_log":PRIVATE_TEXT in json.dumps(store.actions, default=str)}
    out = {"baseline_commit":"96ba8f8b5ac7b1388ef8d59d5377e844f9f9ac56",
           "fixture":"recording store; scripted neighbours and transaction adapter",
           "database_integration_verified":False, "live_llm_calls":0, "cases":cases}
    rendered = json.dumps(out, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
