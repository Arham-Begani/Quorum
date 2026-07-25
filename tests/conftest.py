"""Shared fixtures.

Unit tests are pure and need no cluster. Integration, scenario and chaos tests
do; they skip with a clear message rather than failing, so a contributor with
no database can still run the fast suite.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _have_db() -> bool:
    from quorum.db.pool import load_env
    load_env()
    return bool(os.environ.get("CRDB_URL", "").strip())


needs_db = pytest.mark.skipif(
    not _have_db(), reason="CRDB_URL not set; see .env.example")


@pytest.fixture(scope="session")
def pool():
    if not _have_db():
        pytest.skip("CRDB_URL not set")
    from quorum.db.pool import crdb_url, make_pool, quorum_dbname
    p = make_pool(crdb_url(), min_size=4, max_size=12,
                  dbname=quorum_dbname(), app_name="quorum-tests")
    yield p
    p.close()


@pytest.fixture(scope="session")
def embedder():
    from quorum.embed.bedrock import Embedder
    return Embedder()


@pytest.fixture(scope="session")
def adjudicator():
    from quorum.detect.tier2 import Adjudicator
    return Adjudicator()


@pytest.fixture
def workspace(pool):
    """A fresh workspace per test, cleaned up afterwards."""
    ws = uuid.uuid4()
    yield ws
    with pool.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_atom WHERE workspace_id = %s", (ws,))
            cur.execute("DELETE FROM memory_conflict WHERE workspace_id = %s", (ws,))
            cur.execute("DELETE FROM action_log WHERE workspace_id = %s", (ws,))


@pytest.fixture
def run_id(pool):
    rid = uuid.uuid4()
    yield rid
    with pool.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DELETE FROM run WHERE run_id = %s", (rid,))
