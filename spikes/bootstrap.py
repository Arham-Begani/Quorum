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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop", action="store_true", help="drop probe/memory_atom first")
    args = ap.parse_args()

    report: dict = {"steps": {}}

    _hr("STEP 2 — connect to CockroachDB and print the server version")
    try:
        # Schema changes in CockroachDB are async jobs and routinely take longer
        # than the 5s workload statement_timeout. Using the workload timeout here
        # cancels the client wait while the job completes anyway, which shows up
        # as a bogus "already exists" on the next attempt. Give DDL real room.
        pool = make_pool(crdb_url(), min_size=1, max_size=4,
                         app_name="quorum-spike-bootstrap", statement_timeout_ms=300_000)
    except SystemExit:
        raise
    except Exception as exc:
        print("FAILED to open a connection pool:\n  " + explain_connect_failure(exc))
        return 2

    try:
        with pool.connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                version = cur.fetchone()[0]
                cur.execute("SELECT current_database(), current_user")
                db, user = cur.fetchone()
                try:
                    cur.execute("SHOW CLUSTER SETTING version")
                    cluster_version = cur.fetchone()[0]
                except psycopg.Error as exc:
                    cluster_version = f"unavailable ({_err(exc)})"
    except Exception as exc:
        print("FAILED on first query:\n  " + explain_connect_failure(exc))
        return 2

    print(f"  server version   : {version}")
    print(f"  cluster version  : {cluster_version}")
    print(f"  database / user  : {db} / {user}")
    report["steps"]["connect"] = {
        "ok": True, "server_version": version,
        "cluster_version": cluster_version, "database": db, "user": user,
    }

    try:
        with pool.connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                if args.drop:
                    for t in ("probe", "memory_atom"):
                        cur.execute(f"DROP TABLE IF EXISTS {t}")
                    print("  dropped probe, memory_atom (--drop)")

                # ---------------------------------------------------------
                _hr("STEP 3 — VECTOR(1024) column, vector index, one ANN query")
                _exec(cur, "CREATE TABLE IF NOT EXISTS probe ("
                           "id UUID PRIMARY KEY DEFAULT gen_random_uuid(), "
                           "embedding VECTOR(1024))")
                print("  probe table with VECTOR(1024) : OK")

                syntax, attempts = try_create_vector_index(cur, "probe", "embedding", "probe_embedding_idx")
                if syntax is None and any(
                    kw in a.get("error", "").lower()
                    for a in attempts for kw in ("cluster setting", "not enabled", "disabled", "feature")
                ):
                    print("  index rejected; trying to enable the vector-index feature flag...")
                    for stmt in ENABLE_SETTINGS:
                        try:
                            _exec(cur, stmt)
                            print(f"    applied: {stmt}")
                        except psycopg.Error as exc:
                            print(f"    could not apply {stmt}: {_err(exc)}")
                    syntax, more = try_create_vector_index(cur, "probe", "embedding", "probe_embedding_idx")
                    attempts += more

                for a in attempts:
                    mark = "OK  " if a["ok"] else "FAIL"
                    print(f"  [{mark}] {a['syntax']}")
                    if not a["ok"]:
                        print(f"         {a['error']}")

                report["steps"]["vector_index"] = {"ok": syntax is not None,
                                                   "syntax": syntax, "attempts": attempts}

                if syntax is None:
                    raise Stop(
                        "CREATE VECTOR INDEX failed under every known syntax.\n"
                        f"  cluster version: {cluster_version}\n"
                        f"  server version : {version}\n"
                        "  Per CLAUDE.md §15.3 / BUILD.md §14 the fallback is brute-force\n"
                        "  cosine over a bounded candidate set. The consistency argument is\n"
                        "  unaffected — but make that call explicitly before building on it."
                    )
                print(f"  vector index created via      : {syntax}")

                # one real ANN query
                vec = to_pg_vector(embed("trip:1:hotel.checkin_date", "check-in is 2026-09-14"))
                working_op = None
                op_attempts = []
                for op in DISTANCE_OPERATORS:
                    try:
                        cur.execute(
                            f"SELECT id FROM probe ORDER BY embedding {op} %s::VECTOR LIMIT 5", (vec,)
                        )
                        cur.fetchall()
                        working_op = op
                        op_attempts.append({"op": op, "ok": True})
                        break
                    except psycopg.Error as exc:
                        op_attempts.append({"op": op, "ok": False, "error": _err(exc)})
                if working_op is None:
                    raise Stop("No vector distance operator worked: "
                               + "; ".join(f"{a['op']}: {a.get('error')}" for a in op_attempts))
                print(f"  ANN query ran with operator   : {working_op}")
                report["steps"]["ann_query"] = {"ok": True, "operator": working_op,
                                                "attempts": op_attempts}

                # ---------------------------------------------------------
                _hr("STEP 4 — minimal memory_atom + vector index + partial index")
                _exec(cur, MEMORY_ATOM_DDL)
                print("  memory_atom                   : OK")
                _exec(cur, SUBJECT_LIVE_INDEX_DDL)
                print("  idx_atom_subject_live (partial, WHERE valid_to IS NULL) : OK")

                atom_syntax, atom_attempts = try_create_vector_index(
                    cur, "memory_atom", "embedding", "idx_atom_embedding"
                )
                if atom_syntax is None:
                    raise Stop("vector index on memory_atom failed: "
                               + "; ".join(a.get("error", "") for a in atom_attempts))
                print(f"  idx_atom_embedding            : OK ({atom_syntax})")
                cur.execute("SELECT create_statement FROM [SHOW CREATE TABLE memory_atom]")
                create_stmt = cur.fetchone()[0]
                index_lines = [ln.strip().rstrip(",") for ln in create_stmt.splitlines()
                               if "INDEX" in ln.upper()]
                for ln in index_lines:
                    print(f"    {ln}")
                report["steps"]["memory_atom"] = {
                    "ok": True, "vector_index_syntax": atom_syntax,
                    "attempts": atom_attempts, "indexes": index_lines,
                }

                # verify the ANN + exact query shape the spike actually uses
                cur.execute(
                    f"SELECT id FROM memory_atom WHERE workspace_id = gen_random_uuid() "
                    f"AND valid_to IS NULL ORDER BY embedding {working_op} %s::VECTOR LIMIT 8",
                    (vec,),
                )
                cur.fetchall()
                print("  neighbourhood query shape     : OK")

                # ---------------------------------------------------------
                _hr("STEP 5 — GC TTL and AS OF SYSTEM TIME")
                zone_ok, zone_err = True, None
                try:
                    _exec(cur, "ALTER TABLE memory_atom CONFIGURE ZONE USING gc.ttlseconds = 90000")
                    print("  CONFIGURE ZONE gc.ttlseconds = 90000 : OK (~25h)")
                except psycopg.Error as exc:
                    zone_ok, zone_err = False, _err(exc)
                    print(f"  CONFIGURE ZONE gc.ttlseconds = 90000 : FAILED\n         {zone_err}")

                zone_shown = None
                try:
                    cur.execute("SHOW ZONE CONFIGURATION FOR TABLE memory_atom")
                    zone_shown = cur.fetchall()
                    for row in zone_shown:
                        for line in str(row[-1]).splitlines():
                            if "ttlseconds" in line:
                                print(f"  verified in zone config       : {line.strip()}")
                except psycopg.Error as exc:
                    print(f"  SHOW ZONE CONFIGURATION unavailable: {_err(exc)}")

                aost = {}
                for interval in ("-10s", "-1h"):
                    try:
                        cur.execute(
                            f"SELECT count(*) FROM memory_atom AS OF SYSTEM TIME '{interval}'"
                        )
                        n = cur.fetchone()[0]
                        aost[interval] = {"ok": True, "rows": n}
                        print(f"  AS OF SYSTEM TIME '{interval}'  : OK ({n} rows)")
                    except psycopg.Error as exc:
                        detail = _err(exc)
                        too_young = "does not exist" in detail.lower()
                        aost[interval] = {"ok": False, "error": detail,
                                          "likely_table_too_young": too_young}
                        note = " (table is younger than the interval, not a GC failure)" if too_young else ""
                        print(f"  AS OF SYSTEM TIME '{interval}'  : FAILED{note}\n         {detail}")

                report["steps"]["gc_ttl"] = {
                    "configure_zone_ok": zone_ok, "configure_zone_error": zone_err,
                    "zone_config": [str(r) for r in (zone_shown or [])],
                    "as_of_system_time": aost,
                }

                report["distance_operator"] = working_op
                report["vector_index_syntax"] = atom_syntax

    except Stop as stop:
        print(f"\nSTOP\n{stop}")
        report["stopped"] = str(stop)
        REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nwrote {REPORT_PATH}")
        return 3
    except Exception as exc:
        print(f"\nUNEXPECTED FAILURE: {_err(exc)}")
        report["stopped"] = _err(exc)
        REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 4
    finally:
        pool.close()

    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    _hr("SETUP COMPLETE")
    print(f"  distance operator : {report['distance_operator']}")
    print(f"  vector index      : {report['vector_index_syntax']}")
    print(f"  report            : {REPORT_PATH}")
    print("\n  next: python spikes/prove_race.py --iterations 200 --delay-ms 50")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
