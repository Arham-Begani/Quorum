"""Embedder — Bedrock Titan v2, with cache, backoff, and an offline fallback.

Provider selection is explicit and always reported:

    bedrock_titan      real Titan v2 via boto3 (needs AWS credentials)
    synthetic_offline  deterministic stand-in (quorum/embed/synthetic.py)

`Embedder.provider` is written into every run report so a result can never be
mistaken for one it isn't. Nothing here silently degrades.
"""

from __future__ import annotations

import json
import os
import random
import time

from ..db.metrics import metrics
from .cache import EmbeddingCache, cache_key
from . import synthetic

DEFAULT_MODEL_ID = "amazon.titan-embed-text-v2:0"
DEFAULT_DIM = 1024
MAX_THROTTLE_RETRIES = 5


class EmbeddingError(RuntimeError):
    pass


class Embedder:
    def __init__(
        self,
        *,
        model_id: str | None = None,
        dim: int | None = None,
        region: str | None = None,
        cache: EmbeddingCache | None = None,
        force_offline: bool = False,
    ):
        self.model_id = model_id or os.environ.get("BEDROCK_EMBED_MODEL_ID", DEFAULT_MODEL_ID)
        self.dim = int(dim or os.environ.get("BEDROCK_EMBED_DIM", DEFAULT_DIM))
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self.cache = cache if cache is not None else EmbeddingCache()
        self._client = None
        self.provider = synthetic.PROVIDER_NAME if force_offline else self._select_provider()

    def _select_provider(self) -> str:
        try:
            import boto3
            from botocore.exceptions import BotoCoreError, NoCredentialsError
        except ImportError:
            return synthetic.PROVIDER_NAME
        try:
            session = boto3.session.Session(region_name=self.region)
            if session.get_credentials() is None:
                return synthetic.PROVIDER_NAME
            self._client = session.client("bedrock-runtime")
            return "bedrock_titan"
        except (BotoCoreError, NoCredentialsError, Exception):
            return synthetic.PROVIDER_NAME

    @property
    def is_offline(self) -> bool:
        return self.provider == synthetic.PROVIDER_NAME

    # -- public ---------------------------------------------------------
    def embed(self, text: str) -> tuple[float, ...]:
        key = cache_key(text, self.model_id if not self.is_offline else self.provider, self.dim)
        hit = self.cache.get(key)
        if hit is not None:
            if len(hit) != self.dim:
                raise EmbeddingError(
                    f"cached vector has dim {len(hit)}, expected {self.dim}. "
                    "Changing BEDROCK_EMBED_DIM means re-embedding everything."
                )
            return hit

        if self.is_offline:
            vec = synthetic.embed(text, self.dim)
            metrics.count_embed(0.0, tokens=0)
        else:
            vec = self._embed_bedrock(text)

        if len(vec) != self.dim:
            raise EmbeddingError(f"provider returned dim {len(vec)}, expected {self.dim}")
        self.cache.put(key, vec)
        return vec

    def embed_batch(self, texts: list[str]) -> list[tuple[float, ...]]:
        # Titan v2 has no true batch endpoint; cache per item and loop.
        return [self.embed(t) for t in texts]

    # -- bedrock --------------------------------------------------------
    def _embed_bedrock(self, text: str) -> tuple[float, ...]:
        body = json.dumps({"inputText": text, "dimensions": self.dim, "normalize": True})
        backoff = 0.25
        last_exc: Exception | None = None

        for attempt in range(MAX_THROTTLE_RETRIES):
            t0 = time.perf_counter()
            try:
                resp = self._client.invoke_model(  # type: ignore[union-attr]
                    modelId=self.model_id, body=body,
                    accept="application/json", contentType="application/json",
                )
                payload = json.loads(resp["body"].read())
                vec = tuple(float(x) for x in payload["embedding"])
                metrics.count_embed((time.perf_counter() - t0) * 1000.0,
                                    tokens=int(payload.get("inputTextTokenCount", 0)))
                return vec
            except Exception as exc:  # noqa: BLE001 - inspect then re-raise
                last_exc = exc
                name = type(exc).__name__
                retryable = "Throttl" in name or "TooManyRequests" in name
                if not retryable or attempt == MAX_THROTTLE_RETRIES - 1:
                    break
                time.sleep(backoff * (2 ** attempt) * (0.5 + random.random()))

        # A throttle must never silently become a missed contradiction. Raise;
        # the caller decides, and the caller's decision is to fail closed.
        raise EmbeddingError(f"Bedrock embedding failed: {last_exc!r}") from last_exc

    def info(self) -> dict:
        return {
            "provider": self.provider,
            "model_id": self.model_id if not self.is_offline else None,
            "dim": self.dim,
            "region": self.region if not self.is_offline else None,
            "cache": self.cache.stats(),
        }
