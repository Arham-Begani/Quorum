"""Apply sql/*.sql to the cluster, in order, one statement at a time.

One statement at a time is not fussiness: CockroachDB refuses CREATE DATABASE
inside a multi-statement transaction, and psycopg sends a semicolon-separated
batch as exactly that. So we split and send individually, with a DDL-length
statement timeout because schema changes are async jobs.

    python -m quorum.db.migrate            # apply everything
    python -m quorum.db.migrate --reset    # DROP DATABASE first (destructive)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg

from .pool import (
    DDL_STATEMENT_TIMEOUT_MS,
    crdb_url,
    explain_connect_failure,
    make_pool,
    quorum_dbname,
)

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"


def split_statements(sql: str) -> list[str]:
    """Split on top-level semicolons, ignoring those inside quotes or comments."""
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    in_line_comment = in_block_comment = False
    quote: str | None = None

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            buf.append(ch)
        elif in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                buf.append(ch)
                buf.append(nxt)
                i += 2
                continue
            buf.append(ch)
        elif quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
        elif ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return [s for s in out if not _is_only_comments(s)]


def _is_only_comments(stmt: str) -> bool:
    for line in stmt.splitlines():
        line = line.strip()
        if line and not line.startswith("--"):
            return False
    return True


def _first_line(stmt: str) -> str:
    for line in stmt.splitlines():
        line = line.strip()
        if line and not line.startswith("--"):
            return line[:88]
    return stmt[:88]


def apply_file(cur, path: Path) -> tuple[int, int]:
    applied = skipped = 0
    for stmt in split_statements(path.read_text(encoding="utf-8")):
        try:
            cur.execute(stmt)  # type: ignore[arg-type]
            applied += 1
            print(f"    OK   {_first_line(stmt)}")
        except psycopg.Error as exc:
            detail = str(exc).splitlines()[0]
            if "already exists" in detail.lower():
                skipped += 1
                print(f"    skip {_first_line(stmt)}  ({detail})")
            else:
                print(f"    FAIL {_first_line(stmt)}")
                print(f"         [{exc.sqlstate}] {detail}")
                raise
    return applied, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true",
                    help="DROP DATABASE before applying. Destroys all memory.")
    args = ap.parse_args()

    db = quorum_dbname()
    url = crdb_url()

    # Phase 1: connect to whatever database the URL names, to create ours.
    try:
        boot = make_pool(url, min_size=1, max_size=2, app_name="quorum-migrate",
                         statement_timeout_ms=DDL_STATEMENT_TIMEOUT_MS)
    except Exception as exc:
        print("FAILED to connect:\n  " + explain_connect_failure(exc))
        return 2

    with boot.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            if args.reset:
                print(f"  DROP DATABASE {db} CASCADE")
                cur.execute(f"DROP DATABASE IF EXISTS {db} CASCADE")
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {db}")
            print(f"  database ready: {db}")
    boot.close()

    # Phase 2: everything else, connected to the quorum database.
    pool = make_pool(url, min_size=1, max_size=2, app_name="quorum-migrate",
                     statement_timeout_ms=DDL_STATEMENT_TIMEOUT_MS, dbname=db)
    total_applied = total_skipped = 0
    try:
        with pool.connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT current_database()")
                print(f"  connected to: {cur.fetchone()[0]}")
                for path in sorted(SQL_DIR.glob("*.sql")):
                    print(f"\n  {path.name}")
                    a, s = apply_file(cur, path)
                    total_applied += a
                    total_skipped += s
    finally:
        pool.close()

    print(f"\nmigration complete: {total_applied} applied, {total_skipped} already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
