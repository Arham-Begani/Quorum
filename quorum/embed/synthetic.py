"""Deterministic offline embedding provider — DEVELOPMENT STAND-IN.

This is NOT a semantic model. It is a hash-based vector generator that
reproduces the two properties the memory layer structurally depends on:

  * same text -> same vector (so runs are reproducible, I9)
  * claims sharing a subject_key land close together, others land far apart
    (so ANN neighbourhood retrieval returns plausible conflict candidates)

It exists so the repository runs end to end with no AWS account, which matters
for a judge cloning the repo cold. It cannot judge meaning: two claims that
contradict each other in words but share no subject_key will NOT be near each
other here, whereas real Titan embeddings would place them together.

Whenever this provider is used, the run report records
`embed_provider: "synthetic_offline"`, so no result can be mistaken for one
produced by Bedrock. Set AWS credentials and BEDROCK_EMBED_MODEL_ID to use the
real thing.
"""

from __future__ import annotations

import hashlib
import math
import random

PROVIDER_NAME = "synthetic_offline"
CLAIM_JITTER = 0.28


def _seed(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


def _unit(seed: int, dim: int) -> list[float]:
    rng = random.Random(seed)
    v = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def _anchor_of(text: str) -> str:
    """Group by the subject_key prefix when the text carries one.

    Claim.embed_text() is "<subject_key> <predicate> <object_text>", so the
    first whitespace-delimited token is the subject key.
    """
    return text.split(" ", 1)[0] if " " in text else text


def embed(text: str, dim: int = 1024) -> tuple[float, ...]:
    anchor = _unit(_seed(f"subject::{_anchor_of(text)}"), dim)
    jitter = _unit(_seed(f"claim::{text}"), dim)
    v = [a + CLAIM_JITTER * j for a, j in zip(anchor, jitter)]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return tuple(x / norm for x in v)
