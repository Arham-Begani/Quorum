"""Counters and timers. Product Readiness is a scored criterion, and
"we never measured it" scores zero. [I10]

Everything that costs money or latency is counted here: transactions, 40001
retries, embedding calls, embedding cache hits, tier-2 adjudicator calls. The
run report (quorum/harness/report.py) serialises this straight to JSON.

Thread-safe because the swarm writes concurrently.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from contextlib import contextmanager

# Rough public pricing, USD. Only used to put an order-of-magnitude number in
# the run report; it is labelled as an estimate everywhere it surfaces.
TITAN_V2_USD_PER_1K_TOKENS = 0.00002
CLAUDE_HAIKU_USD_PER_1K_IN = 0.00025
CLAUDE_HAIKU_USD_PER_1K_OUT = 0.00125


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round(p * (len(s) - 1))))
    return s[idx]


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.retries: dict[str, int] = defaultdict(int)
            self.give_ups: dict[str, int] = defaultdict(int)
            self.txn_count: dict[str, int] = defaultdict(int)
            self.durations_ms: dict[str, list[float]] = defaultdict(list)
            self.slow_txns: list[dict] = []

            self.embed_calls = 0
            self.embed_cache_hits = 0
            self.embed_tokens = 0
            self.embed_ms: list[float] = []

            self.adjudicator_calls = 0
            self.adjudicator_failures = 0
            self.adjudicator_ms: list[float] = []
            self.adjudicator_tokens_in = 0
            self.adjudicator_tokens_out = 0

            self.unmapped_attributes: dict[str, int] = defaultdict(int)

    # ---- transactions -------------------------------------------------
    def count_retry(self, label: str) -> None:
        with self._lock:
            self.retries[label] += 1

    def count_txn_give_up(self, label: str) -> None:
        with self._lock:
            self.give_ups[label] += 1

    def observe_txn(self, label: str, seconds: float, attempts: int,
                    slow_ms: float | None = None) -> None:
        ms = seconds * 1000.0
        with self._lock:
            self.txn_count[label] += 1
            self.durations_ms[label].append(ms)
            if slow_ms is not None and ms > slow_ms:
                self.slow_txns.append({"label": label, "ms": round(ms, 1),
                                       "attempts": attempts})

    # ---- embeddings ---------------------------------------------------
    def count_embed(self, ms: float, tokens: int = 0) -> None:
        with self._lock:
            self.embed_calls += 1
            self.embed_ms.append(ms)
            self.embed_tokens += tokens

    def count_embed_cache_hit(self) -> None:
        with self._lock:
            self.embed_cache_hits += 1

    # ---- tier-2 adjudicator -------------------------------------------
    def count_adjudication(self, ms: float, tokens_in: int = 0,
                           tokens_out: int = 0, failed: bool = False) -> None:
        with self._lock:
            self.adjudicator_calls += 1
            self.adjudicator_ms.append(ms)
            self.adjudicator_tokens_in += tokens_in
            self.adjudicator_tokens_out += tokens_out
            if failed:
                self.adjudicator_failures += 1

    def count_unmapped_attribute(self, attribute: str) -> None:
        with self._lock:
            self.unmapped_attributes[attribute] += 1

    # ---- rollups ------------------------------------------------------
    def total_retries(self) -> int:
        with self._lock:
            return sum(self.retries.values())

    def total_give_ups(self) -> int:
        with self._lock:
            return sum(self.give_ups.values())

    def embed_cache_hit_rate(self) -> float:
        with self._lock:
            total = self.embed_calls + self.embed_cache_hits
            return (self.embed_cache_hits / total) if total else 0.0

    def est_cost_usd(self) -> float:
        with self._lock:
            return round(
                self.embed_tokens / 1000 * TITAN_V2_USD_PER_1K_TOKENS
                + self.adjudicator_tokens_in / 1000 * CLAUDE_HAIKU_USD_PER_1K_IN
                + self.adjudicator_tokens_out / 1000 * CLAUDE_HAIKU_USD_PER_1K_OUT,
                6,
            )

    def write_latencies(self) -> list[float]:
        with self._lock:
            out: list[float] = []
            for vals in self.durations_ms.values():
                out.extend(vals)
            return out

    def snapshot(self) -> dict:
        lat = self.write_latencies()
        with self._lock:
            return {
                "txn_retries": sum(self.retries.values()),
                "txn_give_ups": sum(self.give_ups.values()),
                "txn_count": sum(self.txn_count.values()),
                "retries_by_label": dict(self.retries),
                "slow_txns": list(self.slow_txns[:20]),
                "p50_write_ms": round(_percentile(lat, 0.50), 1),
                "p95_write_ms": round(_percentile(lat, 0.95), 1),
                "p99_write_ms": round(_percentile(lat, 0.99), 1),
                "embed_calls": self.embed_calls,
                "embed_cache_hits": self.embed_cache_hits,
                "embed_p50_ms": round(_percentile(self.embed_ms, 0.50), 1),
                "adjudicator_calls": self.adjudicator_calls,
                "adjudicator_failures": self.adjudicator_failures,
                "adjudicator_p50_ms": round(_percentile(self.adjudicator_ms, 0.50), 1),
                "unmapped_attributes": dict(self.unmapped_attributes),
            } | {
                "embed_cache_hit_rate": round(
                    (self.embed_cache_hits / (self.embed_calls + self.embed_cache_hits))
                    if (self.embed_calls + self.embed_cache_hits) else 0.0, 3),
                "est_cost_usd": round(
                    self.embed_tokens / 1000 * TITAN_V2_USD_PER_1K_TOKENS
                    + self.adjudicator_tokens_in / 1000 * CLAUDE_HAIKU_USD_PER_1K_IN
                    + self.adjudicator_tokens_out / 1000 * CLAUDE_HAIKU_USD_PER_1K_OUT, 6),
            }


metrics = Metrics()


@contextmanager
def timed():
    """Yield a one-element list that receives elapsed milliseconds."""
    box = [0.0]
    t0 = time.perf_counter()
    try:
        yield box
    finally:
        box[0] = (time.perf_counter() - t0) * 1000.0
