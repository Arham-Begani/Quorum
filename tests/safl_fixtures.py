"""SAFL 2026-10-30: deterministic recording fixtures, NOT a SQL database.

Only the repository's named write statements and action/recall row lookups
are modelled. ANN and transaction boundaries are supplied explicitly by tests.
This fixture cannot establish isolation, persistence, query plans or SQL validity.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from quorum.db.txn import TxnOutcome
from quorum.detect.tier2 import Adjudicator
from quorum.memory.base import (
    INSERT_ATOM_SQL, INSERT_CONFLICT_SQL, INSERT_ACTION_SQL,
    CONTEST_SQL, SUPERSEDE_SQL, REINFORCE_SQL,
)
from quorum.memory.quorum import QuorumMemory
from quorum.memory.schema import ATOM_COLUMNS, Atom, Claim

WS = uuid.UUID("00000000-0000-0000-0000-000000000001")
KEY = "trip:1:hotel.checkin_date"
PRIVATE_TEXT = "SYNTHETIC-PRIVATE-2031-04-18"


def atom(*, index=1, role="research_agent", agent="research-1", **kwargs):
    values = dict(id=uuid.UUID(int=100 + index), workspace_id=WS,
                  subject_key=KEY, predicate="equals", object_text=PRIVATE_TEXT,
                  object_json={"date": PRIVATE_TEXT}, writer_agent_id=agent,
                  writer_role=role, confidence=0.7, evidence_count=1, distance=0.1)
    values.update(kwargs)
    return Atom(**values)


def claim(*, role="booking_agent", confidence=0.9, **kwargs):
    # A different subject key deliberately requires tier-2 adjudication.
    values = dict(workspace_id=WS, subject_key="trip:1:arrival.date", predicate="equals",
                  object_text="Synthetic arrival", object_json={"date":"2031-04-19"},
                  agent_id="incoming-1", role=role, confidence=confidence)
    values.update(kwargs)
    return Claim(**values)


class RecordingStore:
    def __init__(self, atoms=()):
        self.atoms = {a.id: a for a in atoms}
        self.conflicts = []
        self.actions = []
        self.statements = []
        self.result = []
        self.autocommit = True

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def connection(self): return self
    def cursor(self): return self

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if sql == INSERT_ATOM_SQL:
            self.atoms[params["id"]] = Atom(
                id=params["id"], workspace_id=params["ws"], subject_key=params["sk"],
                predicate=params["pred"], object_text=params["text"],
                object_json=json.loads(params["json"]) if params["json"] else None,
                writer_agent_id=params["agent"], writer_role=params["role"],
                confidence=params["conf"], evidence_count=params["evidence"],
                status=params["status"], visibility=params["visibility"])
        elif sql in (CONTEST_SQL, SUPERSEDE_SQL, REINFORCE_SQL):
            for value in params["ids"]:
                ident = uuid.UUID(str(value))
                old = self.atoms[ident]
                if sql == CONTEST_SQL:
                    self.atoms[ident] = replace(old, status="contested")
                elif sql == SUPERSEDE_SQL:
                    self.atoms[ident] = replace(old, status="superseded",
                        superseded_by=params["new_id"],
                        valid_to=datetime(2031, 1, 1, tzinfo=timezone.utc))
                else:
                    self.atoms[ident] = replace(old, evidence_count=old.evidence_count + 1,
                        confidence=max(old.confidence, params["conf"]))
        elif sql == INSERT_CONFLICT_SQL:
            self.conflicts.append(dict(params))
        elif sql == INSERT_ACTION_SQL:
            self.actions.append(dict(params))
        elif "FROM memory_atom" in sql:
            ws, keys = params
            keys = [keys] if isinstance(keys, str) else keys
            rows = [a for a in self.atoms.values() if a.workspace_id == ws
                    and a.subject_key in keys and a.valid_to is None]
            if "status IN ('active','contested')" in sql:
                rows = [a for a in rows if a.status in ("active", "contested")]
            self.result = [tuple(getattr(a, name) for name in ATOM_COLUMNS) for a in rows]
        else:
            raise AssertionError("Unmodelled SQL in recording fixture: " + sql)

    def fetchall(self): return self.result


class SyntheticEmbedder:
    def embed(self, _text): return [1.0]


def memory(atoms=(), *, adjudicator=None, **cfg):
    store = RecordingStore(atoms)
    mem = QuorumMemory(store, SyntheticEmbedder(),
                       {"adjudicator": adjudicator or Adjudicator(force_offline=True), **cfg})
    mem._registry = {}
    return mem, store


def direct_transaction(pool, fn, **_kwargs):
    """Execute production callback; no real SQL transaction, retry or commit."""
    return TxnOutcome(fn(pool), retries=0, duration_ms=0)


def remember_with_neighbours(mem, incoming, neighbours):
    with patch.object(mem, "_neighbourhood", return_value=neighbours), \
         patch("quorum.memory.quorum.run_txn", direct_transaction):
        return mem.remember(incoming)
