"""Access-control tests. Cheap points that most entrants will skip.

Cross-workspace leakage is a demo-ending bug and a Product Readiness score
killer, so it gets a negative test that asserts zero rows rather than a
positive test that asserts the happy path. [I7]
"""

from __future__ import annotations

import uuid

import pytest

from quorum.memory.factory import make_memory
from quorum.memory.schema import Action, AgentCtx, Atom, Claim

from ..conftest import needs_db

pytestmark = needs_db


def _claim(ws, key="trip:1:hotel.checkin_date", value="2026-09-14",
           visibility="workspace", agent="lodging-1", role="lodging_agent"):
    return Claim(ws, key, "equals", f"check-in is {value}", {"date": value},
                 agent, role, 0.7, visibility)


def test_cross_workspace_read_returns_zero_rows(pool, embedder):
    """The negative test. Memory written in one workspace must be invisible in
    another, no matter how semantically similar the query."""
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    mem = make_memory("quorum", pool, embedder, {})
    try:
        mem.remember(_claim(ws_a))
        agent = AgentCtx("lodging-1", "lodging_agent", 3)

        own = mem.recall("hotel check in date", agent=agent, workspace_id=ws_a)
        assert own, "sanity: the atom must be visible in its own workspace"

        leaked = mem.recall("hotel check in date", agent=agent, workspace_id=ws_b)
        assert leaked == [], f"cross-workspace leakage: {[a.to_dict() for a in leaked]}"

        by_key = mem.recall("", agent=agent, workspace_id=ws_b,
                            subject_keys=["trip:1:hotel.checkin_date"])
        assert by_key == [], "cross-workspace leakage via exact subject_key lookup"
    finally:
        _cleanup(pool, ws_a, ws_b)


def test_action_gate_is_workspace_scoped(pool, embedder):
    """An action in workspace B must not be justified by memory from A."""
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    mem = make_memory("quorum", pool, embedder, {})
    try:
        mem.remember(_claim(ws_a))
        res = mem.act(Action(ws_b, "lodging-1", "book_hotel", {},
                             ("trip:1:hotel.checkin_date",)))
        assert res.gate_result == "blocked_missing"
        assert res.justifying_atom_ids == ()
    finally:
        _cleanup(pool, ws_a, ws_b)


def test_private_visibility_is_not_readable_by_another_agent(pool, embedder):
    ws = uuid.uuid4()
    mem = make_memory("quorum", pool, embedder, {})
    try:
        mem.remember(_claim(ws, visibility="private", agent="research-1",
                            role="research_agent"))
        owner = AgentCtx("research-1", "research_agent", 4)
        other = AgentCtx("lodging-1", "lodging_agent", 3, visibility_scopes=())

        assert mem.recall("", agent=owner, workspace_id=ws,
                          subject_keys=["trip:1:hotel.checkin_date"])
        assert mem.recall("", agent=other, workspace_id=ws,
                          subject_keys=["trip:1:hotel.checkin_date"]) == []
    finally:
        _cleanup(pool, ws)
