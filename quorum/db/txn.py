"""run_txn() — THE ONLY commit path in Quorum. [CLAUDE.md I3]

CockroachDB returns SQLSTATE 40001 (`serialization_failure`) under contention
*by design*: it is the database telling you that committing would have broken
serializability. That is not an error to hide, it is the mechanism that makes
the whole Quorum argument work. So this helper:

  - runs every unit of work at explicit SERIALIZABLE isolation,
  - retries on 40001 with bounded exponential backoff plus jitter,
  - COUNTS the retries and RETURNS them to the caller, so they can be printed,
    charted, and put on camera (CLAUDE.md §9, §15.4).

Never call `conn.commit()` anywhere else.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Any, Callable

import psycopg

from .metrics import metrics

RETRYABLE = "40001"

DEFAULT_MAX_RETRIES = 8
DEFAULT_BASE_BACKOFF_S = 0.02

# Transactions slower than this get logged with their label. Long transactions
# under a swarm workload cause retry storms. (CLAUDE.md §9)
DEFAULT_SLOW_TXN_MS = float(os.environ.get("TXN_SLOW_MS", "100"))


@dataclass(frozen=True)
class TxnOutcome:
    """What the transaction produced, and what it cost to get there."""

    value: Any
    retries: int          # number of 40001 retries before the commit that stuck
    duration_ms: float    # wall clock across ALL attempts, including backoff

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"TxnOutcome(retries={self.retries}, duration_ms={self.duration_ms:.1f})"


def _is_retryable(exc: psycopg.Error) -> bool:
    return getattr(exc, "sqlstate", None) == RETRYABLE


def run_txn(
    pool,
    fn: Callable[[psycopg.Cursor], Any],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    label: str = "txn",
    base_backoff_s: float = DEFAULT_BASE_BACKOFF_S,
) -> TxnOutcome:
    """Execute ``fn(cur)`` inside one SERIALIZABLE transaction.

    Retries on 40001 up to ``max_retries`` times, then re-raises — a write that
    cannot be committed is surfaced and counted, never silently dropped.
    """
    attempt = 0
    t_start = time.perf_counter()

    while True:
        t_attempt = time.perf_counter()
        try:
            with pool.connection() as conn:
                conn.autocommit = False
                # Explicit, not assumed. CockroachDB defaults to SERIALIZABLE,
                # but a cluster setting can change that default, and the entire
                # claim of this project depends on the isolation level actually
                # in force. Declare it.
                conn.isolation_level = psycopg.IsolationLevel.SERIALIZABLE
                with conn.cursor() as cur:
                    result = fn(cur)
                conn.commit()

            elapsed = time.perf_counter() - t_start
            metrics.observe_txn(label, time.perf_counter() - t_attempt, attempt,
                                slow_ms=DEFAULT_SLOW_TXN_MS)
            return TxnOutcome(value=result, retries=attempt, duration_ms=elapsed * 1000.0)

        except psycopg.Error as exc:
            if not _is_retryable(exc):
                raise
            attempt += 1
            metrics.count_retry(label)
            if attempt > max_retries:
                metrics.count_txn_give_up(label)
                raise
            # bounded exponential backoff with full-ish jitter
            time.sleep(base_backoff_s * (2 ** (attempt - 1)) * (0.5 + random.random()))
