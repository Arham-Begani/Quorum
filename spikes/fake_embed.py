"""Deterministic synthetic embedder — SPIKE ONLY.

This exists so the M2 proof spike is free, offline, fast and byte-for-byte
reproducible. It is NOT part of the product. It is replaced wholesale by
Bedrock Titan v2 (`quorum/embed/bedrock.py`) in M4, which produces the real
1024-dim embeddings the system ships with.

What it must guarantee for the spike to be meaningful:

  1. Deterministic  — same text always yields the same vector, so the race
     experiment reproduces exactly (I9).
  2. Right shape    — 1024 dims, unit-normalized, so it drops straight into a
     `VECTOR(1024)` column with no dimension mismatch (CLAUDE.md §15.2).
  3. Semantically shaped — claims about the SAME `subject_key` land close
     together, claims about different subjects land far apart. Without this the
     ANN neighbourhood search would return noise and the spike would prove
     nothing about conflict-candidate retrieval.

Construction: a per-subject "anchor" direction plus a small per-claim jitter.

    v(subject, text) = normalize( anchor(subject) + a * jitter(subject, text) )

With a = 0.28 and 1024 dims, two distinct claims on the same subject sit at
cosine ~= 0.93; claims on different subjects sit at cosine ~= 0.0 (random
high-dimensional vectors are near-orthogonal). That is exactly the structure a
real embedder produces for "check-in is Sep 14" vs "check-in is Sep 15".

Unit-normalized output has a useful side effect: L2 distance and cosine
distance induce the SAME ordering (||a-b||^2 = 2 - 2*cos), so the ANN ordering
is identical whichever operator the cluster's vector index supports.
"""

from __future__ import annotations

import hashlib
import math
import random
from functools import lru_cache

DIM = 1024
CLAIM_JITTER = 0.28


def _seed(s: str) -> int:
    """Stable across processes and platforms — unlike the builtin hash()."""
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


def _unit(seed: int) -> list[float]:
    rng = random.Random(seed)
    v = [rng.gauss(0.0, 1.0) for _ in range(DIM)]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


@lru_cache(maxsize=8192)
def embed(subject_key: str, text: str) -> tuple[float, ...]:
    """Return a deterministic unit vector of length DIM for (subject_key, text)."""
    anchor = _unit(_seed(f"subject::{subject_key}"))
    jitter = _unit(_seed(f"claim::{subject_key}::{text}"))
    v = [a + CLAIM_JITTER * j for a, j in zip(anchor, jitter)]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return tuple(x / norm for x in v)


def to_pg_vector(vec) -> str:
    """Render as a CockroachDB VECTOR literal: '[0.031,-0.017,...]'."""
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


def cosine(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


if __name__ == "__main__":  # quick sanity check, no DB needed
    same_a = embed("trip:1:hotel.checkin_date", "check-in is 2026-09-14")
    same_b = embed("trip:1:hotel.checkin_date", "check-in is 2026-09-15")
    other = embed("trip:1:budget.ceiling_usd", "ceiling is 2400")
    print(f"dim                       = {len(same_a)}")
    print(f"norm                      = {math.sqrt(cosine(same_a, same_a)):.6f}")
    print(f"cos(same subject, diff claim) = {cosine(same_a, same_b):.4f}")
    print(f"cos(diff subject)             = {cosine(same_a, other):.4f}")
    print(f"deterministic                 = {embed('trip:1:hotel.checkin_date', 'check-in is 2026-09-14') == same_a}")
