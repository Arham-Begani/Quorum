"""New SAFL regressions (2026-10-30); real code paths, synthetic I/O only."""
import json
import uuid
from unittest.mock import patch

import pytest

from quorum.detect.tier2 import Adjudicator, BEDROCK
from quorum.memory.factory import make_memory, MODES
from quorum.memory.quorum import PreparedDecision
from quorum.memory.schema import Action, AgentCtx
from quorum.memory.base import SUPERSEDE_SQL, REINFORCE_SQL
from tests.safl_fixtures import (
    WS, KEY, PRIVATE_TEXT, RecordingStore, SyntheticEmbedder, atom, claim,
    memory, remember_with_neighbours,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs): raise AssertionError("Network forbidden in SAFL regressions")
    monkeypatch.setattr("socket.socket.connect", deny)
    monkeypatch.setattr("socket.create_connection", deny)


@pytest.mark.parametrize("incoming_role,existing_role,inc_conf,evidence", [
    ("booking_agent", "research_agent", 0.9, 1),  # R1 supersede on baseline
    ("research_agent", "booking_agent", 0.1, 1),  # R1 reject
    ("ground_agent", "ground_agent", 0.9, 5),     # R2 reject
    ("ground_agent", "ground_agent", 0.9, 1),     # R3 supersede
    ("ground_agent", "ground_agent", 0.7, 1),     # R4 contest control
])
@pytest.mark.parametrize("failure", ["offline", "timeout", "malformed", "throttle", "run_budget"])
def test_adjudication_failure_contests_before_ranking_and_blocks_actions(
        incoming_role, existing_role, inc_conf, evidence, failure):
    old = atom(role=existing_role, evidence_count=evidence, visibility="workspace")
    incoming = claim(role=incoming_role, confidence=inc_conf)
    adj = Adjudicator(force_offline=True)
    if failure != "offline":
        adj.provider = BEDROCK  # No client creation or live invocation.
    if failure == "run_budget":
        adj.calls_this_run = adj.run_ceiling
    errors = {"timeout": TimeoutError("synthetic timeout"),
              "throttle": RuntimeError("synthetic throttle")}
    with patch.object(adj, "_invoke", side_effect=errors.get(failure),
                      return_value=("not-json", 0, 0)) as invoke:
        mem, store = memory([old], adjudicator=adj)
        result = remember_with_neighbours(mem, incoming, [old])
    assert result.error is None
    assert result.resolution == "contest"
    assert result.policy_rule == "fail_closed"
    assert {a.status for a in store.atoms.values()} == {"contested"}
    assert all(a.valid_to is None and a.superseded_by is None for a in store.atoms.values())
    assert not any(sql in (SUPERSEDE_SQL, REINFORCE_SQL) for sql, _ in store.statements)
    assert store.conflicts[0]["resolution"] == "contest"
    assert store.conflicts[0]["rule"] == "fail_closed"
    assert "fail-closed" in store.conflicts[0]["rationale"]
    if failure in ("offline", "run_budget"):
        invoke.assert_not_called()
    for key in (KEY, incoming.subject_key):
        gate = mem.act(Action(WS, "booking-1", "synthetic_booking", {}, (key,)))
        assert gate.gate_result == "blocked_contested"
        assert not gate.executed and not gate.justifying_atom_ids


def test_per_claim_budget_exhaustion_is_not_authority_ranking():
    old = atom()
    first = atom(index=2, subject_key="trip:1:other.fact")
    adj = Adjudicator(force_offline=True, per_claim_budget=1)
    adj.provider = BEDROCK
    with patch.object(adj, "_invoke", return_value=(
            '{"verdict":"unrelated","confidence":1,"rationale":"synthetic control"}', 0, 0)) as invoke:
        mem, store = memory([first, old], adjudicator=adj)
        result = remember_with_neighbours(mem, claim(), [first, old])
    assert invoke.call_count == 1
    assert result.resolution == "contest"
    assert store.atoms[old.id].status == "contested"
    assert store.atoms[first.id].status == "active"
    assert "budget exhausted" in store.conflicts[1]["rationale"]


@pytest.mark.parametrize("prepared_verdict", [None, "agreement", "refinement", "unrelated"])
def test_explicit_force_contest_precedes_prepared_or_structural_verdict(prepared_verdict):
    old = atom()
    mem, store = memory([old])
    prepared = PreparedDecision("[1]")
    if prepared_verdict:
        prepared.verdicts[old.id] = (prepared_verdict, "tier2_semantic", 0.995, 0)
    with patch.object(mem, "_neighbourhood", return_value=[old]):
        plan = mem._commit(store, claim(), prepared, uuid.UUID(int=999), {},
                           force_contest=(old.id,))
    assert plan.resolution == "contest" and plan.policy_rule == "fail_closed"
    assert not plan.supersede_ids and not plan.reinforce_ids
    assert old.id in plan.contest_ids
    assert {a.status for a in store.atoms.values()} == {"contested"}


def test_reprepare_exhaustion_executes_production_fallback_and_contests():
    from tests.safl_fixtures import direct_transaction
    old = atom()
    mem, store = memory([old], reprepare_max=1)
    # Probe misses the neighbour; authoritative read sees it on both attempts.
    with patch.object(mem, "_neighbourhood", side_effect=[[], [old], [], [old], [old]]), \
         patch("quorum.memory.quorum.run_txn", side_effect=direct_transaction) as txn:
        result = mem.remember(claim())
    assert txn.call_count == 3
    assert result.resolution == "contest" and result.error is None
    assert {a.status for a in store.atoms.values()} == {"contested"}
    assert mem.act(Action(WS, "booking-1", "synthetic_booking", {}, (KEY,))).gate_result == "blocked_contested"


@pytest.mark.parametrize("verdict,expected", [("agreement", "reinforce"),
    ("refinement", "supersede"), ("unrelated", "accept"), ("contradiction", "supersede")])
def test_successful_adjudication_keeps_existing_policy_behavior(verdict, expected):
    old = atom()
    adj = Adjudicator(force_offline=True)
    adj.provider = BEDROCK
    with patch.object(adj, "_invoke", return_value=(json.dumps(
            {"verdict":verdict,"confidence":0.9,"rationale":"synthetic valid result"}), 0, 0)):
        mem, store = memory([old], adjudicator=adj)
        result = remember_with_neighbours(mem, claim(), [old])
    assert result.resolution == expected and result.error is None
    assert result.policy_rule != "fail_closed"


def test_uncertainty_on_one_neighbour_prevents_other_supersession():
    uncertain = atom(index=1)
    structural = atom(index=2, subject_key=claim().subject_key)
    mem, store = memory([uncertain, structural])
    result = remember_with_neighbours(mem, claim(), [structural, uncertain])
    assert result.resolution == "contest"
    assert store.atoms[uncertain.id].status == "contested"
    assert store.atoms[structural.id].status == "active"
    assert store.atoms[structural.id].superseded_by is None


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("status", ["active", "contested"])
def test_another_agents_private_value_cannot_justify_or_leak_into_action(mode, status):
    old = atom(visibility="private", status=status)
    store = RecordingStore([old])
    mem = make_memory(mode, store, SyntheticEmbedder(),
                      {"adjudicator": Adjudicator(force_offline=True)})
    ctx = AgentCtx("lodging-1", "lodging_agent", 3, visibility_scopes=())
    assert mem.recall("", agent=ctx, workspace_id=WS, subject_keys=[KEY]) == []
    result = mem.act(Action(WS, ctx.agent_id, "synthetic_booking", {}, (KEY,)))
    assert result.gate_result == "blocked_missing"
    assert not result.executed and not result.justifying_atom_ids
    assert PRIVATE_TEXT not in json.dumps(result.to_dict())
    assert PRIVATE_TEXT not in json.dumps(store.actions, default=str)
    assert str(old.id) not in json.dumps(store.actions, default=str)
    assert "resolved" not in json.loads(store.actions[0]["payload"])


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("visibility,actor", [("private", "research-1"),
    ("workspace", "lodging-1")])
def test_owner_private_and_shared_memory_remain_usable(mode, visibility, actor):
    old = atom(visibility=visibility)
    store = RecordingStore([old])
    mem = make_memory(mode, store, SyntheticEmbedder(),
                      {"adjudicator": Adjudicator(force_offline=True)})
    result = mem.act(Action(WS, actor, "synthetic_booking", {}, (KEY,)))
    assert result.allowed and result.executed
    assert result.justifying_atom_ids == (old.id,)
    assert json.loads(store.actions[0]["payload"])["resolved"][KEY] == old.object_json


def test_hidden_private_atom_cannot_poison_or_enter_shared_action_payload():
    hidden = atom(visibility="private", status="contested")
    shared = atom(index=2, object_text="shared", object_json={"date":"2031-05-01"})
    mem, store = memory([hidden, shared])
    result = mem.act(Action(WS, "lodging-1", "synthetic_booking", {}, (KEY,)))
    assert result.allowed and result.justifying_atom_ids == (shared.id,)
    assert PRIVATE_TEXT not in json.dumps(store.actions, default=str)
    assert json.loads(store.actions[0]["payload"])["resolved"][KEY] == shared.object_json


def test_owner_private_contest_still_blocks():
    old = atom(visibility="private", status="contested")
    mem, _ = memory([old])
    result = mem.act(Action(WS, old.writer_agent_id, "synthetic_booking", {}, (KEY,)))
    assert result.gate_result == "blocked_contested" and not result.executed


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("actor", [None, "", " \t", 123])
def test_missing_or_malformed_action_identity_is_rejected_before_queries(mode, actor):
    # Dataclasses do not validate annotations; direct SDK inputs can be malformed.
    store = RecordingStore([atom(visibility="private", agent=actor)])
    mem = make_memory(mode, store, SyntheticEmbedder(),
                      {"adjudicator": Adjudicator(force_offline=True)})
    with pytest.raises(ValueError, match="agent_id"):
        mem.act(Action(WS, actor, "synthetic_booking", {}, (KEY,)))
    assert store.statements == [] and store.actions == []


@pytest.mark.parametrize("owner", [None, "", " \t", 123])
def test_malformed_owner_never_justifies_an_identified_agents_action(owner):
    mem, store = memory([atom(visibility="private", agent=owner)])
    result = mem.act(Action(WS, "valid-actor", "synthetic_booking", {}, (KEY,)))
    assert result.gate_result == "blocked_missing"
    assert PRIVATE_TEXT not in json.dumps(store.actions, default=str)


def test_valid_owner_id_does_not_require_registry_lookup_in_trusted_sdk():
    # This is an explicit LIMIT: a matching valid ID is trusted, not authenticated.
    mem, store = memory([atom(visibility="private", agent="unregistered-owner")])
    result = mem.act(Action(WS, "unregistered-owner", "synthetic_booking", {}, (KEY,)))
    assert result.allowed and result.executed
    assert not any("agent_registry" in sql for sql, _ in store.statements)


@pytest.mark.parametrize("visibility", [None, "PRIVATE", "unrecognized"])
@pytest.mark.parametrize("actor", ["research-1", "other-agent"])
def test_unknown_visibility_is_not_shared_or_owner_readable(visibility, actor):
    mem, store = memory([atom(visibility=visibility)])
    ctx = AgentCtx(actor, "research_agent", 4)
    assert mem.recall("", agent=ctx, workspace_id=WS, subject_keys=[KEY]) == []
    result = mem.act(Action(WS, actor, "synthetic_booking", {}, (KEY,)))
    assert result.gate_result == "blocked_missing"
    assert PRIVATE_TEXT not in json.dumps(store.actions, default=str)


@pytest.mark.parametrize("missing_id", [None, "", " \t"])
def test_two_missing_identities_do_not_grant_private_recall(missing_id):
    mem, _ = memory([atom(visibility="private", agent=missing_id)])
    assert mem.recall("", agent=AgentCtx(missing_id, "research_agent", 4),
                      workspace_id=WS, subject_keys=[KEY]) == []


def test_absent_action_context_has_no_effect():
    mem, store = memory([atom(visibility="private")])
    with pytest.raises((AttributeError, TypeError, ValueError)):
        mem.act(None)
    assert store.statements == [] and store.actions == []


@pytest.mark.parametrize("verdict", ["agreement", "refinement", "contradiction", "unrelated"])
def test_forced_policy_bypasses_ranking_without_any_store_fixture(verdict):
    from quorum.policy.engine import resolve
    old = atom()
    with patch("quorum.policy.engine.resolve_pair", side_effect=AssertionError("ranking must not run")):
        plan = resolve(claim(), [(old, verdict, "tier2_semantic", 0.99, 0)],
                       force_contest={old.id:"fail-closed: synthetic explicit reason"})
    assert plan.resolution == "contest" and plan.policy_rule == "fail_closed"
    assert plan.contest_ids == (old.id,)
    assert plan.supersede_ids == plan.reinforce_ids == ()
    assert plan.conflict_records()[0].rationale == "fail-closed: synthetic explicit reason"


def test_prepare_retains_failure_reason_from_real_offline_adjudicator():
    old = atom()
    mem, _ = memory([old])
    with patch.object(mem, "_neighbourhood", return_value=[old]):
        prepared = mem._prepare(claim())
    assert prepared.failed_closed == {old.id:"fail-closed: no tier-2 adjudicator configured (offline stub)"}


def test_failed_closed_stale_neighbour_does_not_override_authoritative_absence():
    old = atom()
    mem, store = memory([old])
    with patch.object(mem, "_neighbourhood", return_value=[old]):
        prepared = mem._prepare(claim())
    with patch.object(mem, "_neighbourhood", return_value=[]):
        plan = mem._commit(store, claim(), prepared, uuid.UUID(int=999), {})
    assert plan.resolution == "accept" and not plan.contest_ids
