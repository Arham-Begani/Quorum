"""M2 setup: connect, verify the vector index, create the minimal schema, set GC TTL.

Runs BUILD.md §2.1-§2.3 in order and stops at the first hard failure, because
each step is a go/no-go on the one after it. Everything it learns about the
cluster (version, which CREATE VECTOR INDEX syntax was accepted, which distance
operator works, whether zone configs and AS OF SYSTEM TIME are available) is
printed and written to spikes/bootstrap_report.json so prove_race.py can use it
and so the findings are auditable rather than remembered.

    python spikes/bootstrap.py [--drop]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg

if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quorum.db.pool import crdb_url, explain_connect_failure, make_pool  # noqa: E402
from spikes.fake_embed import embed, to_pg_vector  # noqa: E402

REPORT_PATH = Path(__file__).with_name("bootstrap_report.json")

# Candidate syntaxes for the distributed vector index. CLAUDE.md §15.3: this
# has differed across CockroachDB releases, so we try, and we report exactly
# what the cluster accepted instead of assuming.
VECTOR_INDEX_SYNTAXES = [
    ("CREATE VECTOR INDEX", "CREATE VECTOR INDEX {name} ON {table} ({col})"),
    ("USING cspann", "CREATE INDEX {name} ON {table} USING cspann ({col})"),
    ("USING cspann + vector_l2_ops", "CREATE INDEX {name} ON {table} USING cspann ({col} vector_l2_ops)"),
    ("USING cspann + vector_cosine_ops", "CREATE INDEX {name} ON {table} USING cspann ({col} vector_cosine_ops)"),
]

# Vectors from fake_embed are unit-normalized, so L2 and cosine give the same
# ordering; we use whichever operator the cluster supports.
DISTANCE_OPERATORS = ["<->", "<=>"]

ENABLE_SETTINGS = [
    "SET CLUSTER SETTING feature.vector_index.enabled = true",
    "SET CLUSTER SETTING sql.vector_index.enabled = true",
]

MEMORY_ATOM_DDL = """
CREATE TABLE IF NOT EXISTS memory_atom (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id   UUID         NOT NULL,
  subject_key    STRING       NOT NULL,
  predicate      STRING       NOT NULL,
  object_text    STRING       NOT NULL,
  object_json    JSONB,
  embedding      VECTOR(1024) NOT NULL,
  writer_role    STRING       NOT NULL,
  confidence     FLOAT        NOT NULL DEFAULT 0.5,
  valid_from     TIMESTAMPTZ  NOT NULL DEFAULT now(),
  valid_to       TIMESTAMPTZ,
  superseded_by  UUID,
  status         STRING       NOT NULL DEFAULT 'active',
  CONSTRAINT ck_status CHECK (status IN ('active','superseded','contested','rejected')),
  CONSTRAINT ck_conf   CHECK (confidence BETWEEN 0 AND 1)
)
"""

SUBJECT_LIVE_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_atom_subject_live
  ON memory_atom (workspace_id, subject_key)
  WHERE valid_to IS NULL
"""


class Stop(Exception):
    """A gate failed. Report and exit — do not press on."""


def _hr(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def _exec(cur, sql: str) -> None:
    cur.execute(sql)  # type: ignore[arg-type]


def _err(exc: BaseException) -> str:
    state = getattr(exc, "sqlstate", None)
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return f"[{state or type(exc).__name__}] {text}"


def try_create_vector_index(cur, table: str, col: str, name: str) -> tuple[str | None, list[dict]]:
    """Return (syntax_label_that_worked, attempts). None means all failed."""
    attempts: list[dict] = []
    for label, template in VECTOR_INDEX_SYNTAXES:
        sql = template.format(name=name, table=table, col=col)
        try:
            _exec(cur, sql)
            attempts.append({"syntax": label, "sql": sql, "ok": True})
            return label, attempts
        except psycopg.Error as exc:
            detail = _err(exc)
            attempts.append({"syntax": label, "sql": sql, "ok": False, "error": detail})
            if "already exists" in detail.lower():
                return label + " (pre-existing)", attempts
    return None, attempts
