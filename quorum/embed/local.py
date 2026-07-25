"""Local embedding provider — a real semantic model, no cloud account.

Unlike `synthetic.py`, this IS a semantic model. It runs a small sentence
transformer through ONNX on CPU, which means claims that contradict each other
without sharing a subject_key land near each other in vector space — the thing
the synthetic provider structurally cannot do, and the reason S2 is untestable
without it.

Dimension handling is the interesting part. These models emit 384 dimensions
and `memory_atom.embedding` is VECTOR(1024). Rather than migrate the column we
ZERO-PAD, which is exact rather than approximate:

    v  = (a₁ … a₃₈₄)                   ‖v‖ = 1
    v' = (a₁ … a₃₈₄, 0 … 0)            ‖v'‖ = 1

Appending zeros changes neither the norm nor any dot product, so cosine
similarity and L2 distance between two padded vectors are IDENTICAL to those
between the originals. The vector index, the distance operator and the
similarity threshold all keep working untouched. The only cost is storage.

Set `EMBED_PROVIDER=local` to force this, or let it be selected automatically
when Bedrock is unreachable.
"""

from __future__ import annotations

import math
import os
import threading
import time

from ..db.metrics import metrics

PROVIDER_NAME = "local_onnx"

# Small, CPU-friendly, 384-dim. Quality is well short of Titan v2 but it is a
# genuine semantic space, which is the property that matters here.
DEFAULT_MODEL = os.environ.get("LOCAL_EMBED_MODEL", "BAAI/bge-small-en-v1.5")

_model = None
_lock = threading.Lock()
_native_dim: int | None = None


class LocalEmbedUnavailable(RuntimeError):
    pass


def available() -> bool:
    try:
        import fastembed  # noqa: F401
        return True
    except ImportError:
        return False


def _get_model():
    """Load once, lazily. Model load is the expensive part, not inference."""
    global _model, _native_dim
    if _model is not None:
        return _model
    with _lock:
        if _model is not None:
            return _model
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise LocalEmbedUnavailable("fastembed is not installed") from exc
        try:
            _model = TextEmbedding(model_name=DEFAULT_MODEL)
        except Exception as exc:
            raise LocalEmbedUnavailable(
                f"could not load {DEFAULT_MODEL}: {type(exc).__name__}: {exc}") from exc
        _native_dim = None
        return _model


def native_dim() -> int | None:
    return _native_dim


def embed(text: str, dim: int = 1024) -> tuple[float, ...]:
    """Return a unit vector of length `dim`, zero-padded from the model's own."""
    global _native_dim
    model = _get_model()
    t0 = time.perf_counter()
    vec = list(next(iter(model.embed([text]))))
    _native_dim = len(vec)

    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    vec = [float(x) / norm for x in vec]

    if len(vec) > dim:
        # Truncating would break the norm; refuse rather than silently degrade.
        raise LocalEmbedUnavailable(
            f"{DEFAULT_MODEL} emits {len(vec)} dims but the column holds {dim}. "
            "Pick a smaller model or widen the column.")
    if len(vec) < dim:
        vec = vec + [0.0] * (dim - len(vec))

    # Rough token estimate; the local model costs nothing, but the run report
    # should still show where the calls went.
    metrics.count_embed((time.perf_counter() - t0) * 1000.0,
                        tokens=max(1, len(text) // 4))
    return tuple(vec)


def info() -> dict:
    return {"model": DEFAULT_MODEL, "native_dim": _native_dim,
            "loaded": _model is not None}
