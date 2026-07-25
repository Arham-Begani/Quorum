"""Embedder — one interface over three providers, in descending quality.

    bedrock_titan      real Titan v2 via boto3 (needs AWS credentials)
    local_onnx         a real semantic model on CPU (quorum/embed/local.py)
    synthetic_offline  hash-based stand-in, NOT semantic (embed/synthetic.py)

The distinction that matters is `is_semantic`, not "is it cloud". The first two
place semantically related claims near each other, so a contradiction between
claims that share no subject_key can still be surfaced. The third cannot, by
construction -- which is exactly why a scenario depending on that capability is
reported as untested rather than failed when it is in use.

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
from . import local, synthetic

DEFAULT_MODEL_ID = "amazon.titan-embed-text-v2:0"
DEFAULT_DIM = 1024
MAX_THROTTLE_RETRIES = 5


class EmbeddingError(RuntimeError):
    pass


def has_bedrock_auth(session) -> bool:
    """Is there any usable Bedrock credential?

    Two shapes exist. Classic IAM credentials (access key / secret / role), and
    the newer short-form **Bedrock API key**, which the current console hands
    out directly and which botocore reads from AWS_BEARER_TOKEN_BEDROCK. The
    bearer token does NOT show up in session.get_credentials(), so checking
    only that silently falls back to the offline provider even though Bedrock
    is perfectly reachable.
    """
    if os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip():
        return True
    try:
        return session.get_credentials() is not None
    except Exception:
        return False


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
        # Load .env here too. Constructing an Embedder standalone otherwise sees
        # no credentials and quietly picks a lesser provider, while the same
        # object inside the harness -- where make_pool() has already loaded the
        # file -- picks Bedrock. Same code, different provider, depending on
        # what else happened to run first.
        from ..db.pool import load_env
        load_env()
        self.model_id = model_id or os.environ.get("BEDROCK_EMBED_MODEL_ID", DEFAULT_MODEL_ID)
        self.dim = int(dim or os.environ.get("BEDROCK_EMBED_DIM", DEFAULT_DIM))
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self.cache = cache if cache is not None else EmbeddingCache()
        self._client = None
        self.selection_error: str | None = None
        self.provider = synthetic.PROVIDER_NAME if force_offline else self._select_provider()

    def _select_provider(self) -> str:
        """bedrock_titan > local_onnx > synthetic_offline.

        Preference order is quality of semantic space. The synthetic provider is
        last because it is not a semantic model at all -- it cannot place two
        claims near each other unless they already share a subject_key, which is
        the single capability cross-key contradiction detection depends on.
        """
        forced = os.environ.get("EMBED_PROVIDER", "").strip()
        if forced == "local":
            return local.PROVIDER_NAME
        if forced == "synthetic":
            return synthetic.PROVIDER_NAME

        if forced != "bedrock":
            pass  # fall through to auto-detection
        try:
            import boto3
            session = boto3.session.Session(region_name=self.region)
            if has_bedrock_auth(session):
                self._client = session.client("bedrock-runtime")
                # Credentials existing is NOT the same as the service working.
                # A valid IAM key on an account without Bedrock entitlement
                # authenticates fine and refuses every invoke, so selecting on
                # credentials alone reports `bedrock_titan` in the run report
                # while nothing is actually embedded. Prove it end to end.
                self._probe()
                return "bedrock_titan"
        except Exception as exc:
            self._client = None
            self.selection_error = f"{type(exc).__name__}: {str(exc)[:140]}"
        if forced == "bedrock":
            return "bedrock_titan"      # explicit request: fail loudly, not silently
        if local.available():
            return local.PROVIDER_NAME
        return synthetic.PROVIDER_NAME

    def _probe(self) -> None:
        """One real embedding call. Raises if Bedrock is not actually usable."""
        resp = self._client.invoke_model(  # type: ignore[union-attr]
            modelId=self.model_id,
            body=json.dumps({"inputText": "probe", "dimensions": self.dim,
                             "normalize": True}),
            accept="application/json", contentType="application/json",
        )
        json.loads(resp["body"].read())["embedding"]

    @property
    def is_offline(self) -> bool:
        """True when embeddings are NOT a real semantic space.

        `local_onnx` is offline in the sense of needing no cloud account, but it
        IS a semantic model, so it is not "offline" for the purpose of deciding
        whether a cross-key scenario can be tested.
        """
        return self.provider == synthetic.PROVIDER_NAME

    @property
    def is_semantic(self) -> bool:
        return self.provider in ("bedrock_titan", local.PROVIDER_NAME)

    # -- public ---------------------------------------------------------
    def embed(self, text: str) -> tuple[float, ...]:
        # Key on the PROVIDER as well as the model: vectors from different
        # providers live in incomparable spaces, and serving one for the other
        # would silently corrupt every distance in the system.
        key = cache_key(text, f"{self.provider}:{self.model_id}", self.dim)
        hit = self.cache.get(key)
        if hit is not None:
            if len(hit) != self.dim:
                raise EmbeddingError(
                    f"cached vector has dim {len(hit)}, expected {self.dim}. "
                    "Changing BEDROCK_EMBED_DIM means re-embedding everything."
                )
            return hit

        if self.provider == synthetic.PROVIDER_NAME:
            vec = synthetic.embed(text, self.dim)
            metrics.count_embed(0.0, tokens=0)
        elif self.provider == local.PROVIDER_NAME:
            vec = local.embed(text, self.dim)
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
        out = {
            "provider": self.provider,
            "dim": self.dim,
            "is_semantic": self.is_semantic,
            "cache": self.cache.stats(),
        }
        if self.selection_error:
            out["bedrock_unavailable"] = self.selection_error
        if self.provider == "bedrock_titan":
            out |= {"model_id": self.model_id, "region": self.region}
        elif self.provider == local.PROVIDER_NAME:
            out |= {"model_id": local.DEFAULT_MODEL, "native_dim": local.native_dim(),
                    "note": "zero-padded to the column width; cosine is preserved exactly"}
        else:
            out |= {"model_id": None,
                    "note": "NOT a semantic model; cross-key detection is untestable"}
        return out
