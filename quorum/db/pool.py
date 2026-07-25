"""Connection pool with explicit statement timeouts.

Raw psycopg3 on purpose: transaction boundaries are the thesis of this project,
and any framework that manages sessions for you hides exactly the thing that
has to be demonstrated (BUILD.md §0).
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import ConnectionPool

DEFAULT_STATEMENT_TIMEOUT_MS = 5000

# Schema changes in CockroachDB are async jobs and routinely outlast the
# workload statement timeout. Use this for DDL, never for the workload.
DDL_STATEMENT_TIMEOUT_MS = 300_000


def quorum_dbname() -> str:
    load_env()
    return os.environ.get("QUORUM_DB", "quorum")


def load_env(dotenv_path: str | os.PathLike = ".env") -> None:
    """Load .env if present. Real env vars always win."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is optional
        return
    p = Path(dotenv_path)
    if p.exists():
        load_dotenv(p, override=False)


def crdb_url() -> str:
    load_env()
    url = os.environ.get("CRDB_URL", "").strip()
    if not url:
        raise SystemExit(
            "CRDB_URL is not set.\n"
            "  Copy .env.example to .env and paste your CockroachDB Cloud\n"
            "  connection string into CRDB_URL, or export CRDB_URL in the shell."
        )
    return url


def build_conninfo(
    url: str,
    *,
    app_name: str | None = None,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    dbname: str | None = None,
) -> str:
    """Append our session options WITHOUT clobbering options already in the URL.

    CockroachDB Serverless routing lives in `options=--cluster=<routing-id>` on
    some clusters. Passing a bare `options` kwarg would silently drop it and the
    connection would fail in a confusing way, so merge instead of overwrite.

    Also supplies a CA bundle when the URL asks for certificate verification but
    names none. CockroachDB Cloud serves a Let's Encrypt certificate, so any
    standard root store verifies it — but on Windows libpq looks for
    %APPDATA%\\postgresql\\root.crt (absent), and OpenSSL's `system` store is
    typically empty too, so verification fails for want of roots rather than for
    any real trust problem. Point it at certifi's Mozilla bundle instead.

    This keeps verification at FULL strength. It is not a downgrade to
    sslmode=require, and nothing here weakens the TLS check.
    """
    d = conninfo_to_dict(url)
    existing = (d.get("options") or "").strip()
    ours = f"-c statement_timeout={statement_timeout_ms}"
    d["options"] = f"{existing} {ours}".strip()
    d.setdefault("application_name", app_name or os.environ.get("CRDB_APP_NAME", "quorum"))
    if d.get("sslmode") in ("verify-full", "verify-ca") and not d.get("sslrootcert"):
        d["sslrootcert"] = default_ca_bundle()
    if dbname:
        d["dbname"] = dbname
    return make_conninfo(**d)


def default_ca_bundle() -> str:
    """A CA bundle libpq can actually read. Falls back to libpq's own default."""
    try:
        import certifi

        return certifi.where()
    except ImportError:  # pragma: no cover
        return "system"


def preflight(conninfo: str, *, connect_timeout: int = 15) -> None:
    """Open one connection and close it, so failures surface immediately.

    Without this the pool spends its whole timeout retrying and prints the same
    error a dozen times over.
    """
    with psycopg.connect(conninfo, connect_timeout=connect_timeout) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()


def make_pool(
    url: str | None = None,
    *,
    min_size: int = 2,
    max_size: int = 8,
    app_name: str | None = None,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    dbname: str | None = None,
) -> ConnectionPool:
    conninfo = build_conninfo(
        url or crdb_url(), app_name=app_name,
        statement_timeout_ms=statement_timeout_ms, dbname=dbname,
    )
    preflight(conninfo)
    return ConnectionPool(
        conninfo,
        min_size=min_size,
        max_size=max_size,
        timeout=30.0,
        open=True,
        kwargs={"autocommit": False},
    )


def server_version(pool: ConnectionPool) -> str:
    with pool.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            row = cur.fetchone()
    return row[0] if row else "unknown"


def explain_connect_failure(exc: BaseException) -> str:
    """Turn the two connection failures that actually happen into advice."""
    msg = str(exc)
    low = msg.lower()
    hints = []
    if "certificate" in low or "ssl" in low or "root" in low:
        hints.append(
            "SSL/root-certificate problem. On Windows libpq looks for\n"
            "  %APPDATA%\\postgresql\\root.crt when sslmode=verify-full.\n"
            "  Either download the cluster CA cert and add\n"
            "  &sslrootcert=C:/path/to/root.crt to CRDB_URL, or (dev only, and\n"
            "  say so in the README) drop to sslmode=require."
        )
    if "password authentication" in low or "role" in low:
        hints.append("Credentials rejected — re-copy the connection string from the Cloud console.")
    if "timeout" in low or "could not translate" in low or "connection refused" in low:
        hints.append("Host unreachable — check the host/port and that the cluster is awake.")
    return msg + ("\n\nHint:\n  " + "\n  ".join(hints) if hints else "")
