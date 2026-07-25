"""Content-hash embedding cache.

Agents re-assert the same claims constantly ("check-in is 2026-09-14" gets
written by the lodging agent on every turn), so caching cuts both cost and
latency substantially. Keyed by hash(model_id + dim + text) so a model or
dimension change cannot silently serve stale vectors of the wrong shape.

Persisted to disk so repeated scenario runs during development are near-free.
"""

from __future__ import annotations

import hashlib
import json
import struct
import threading
from pathlib import Path

from ..db.metrics import metrics

DEFAULT_CACHE_DIR = Path(".cache/embeddings")


def cache_key(text: str, model_id: str, dim: int) -> str:
    h = hashlib.sha256()
    h.update(model_id.encode("utf-8"))
    h.update(b"\x00")
    h.update(str(dim).encode("utf-8"))
    h.update(b"\x00")
    h.update(text.encode("utf-8"))
    return h.hexdigest()


class EmbeddingCache:
    """Two-level: in-process dict over a small on-disk shard store."""

    def __init__(self, cache_dir: Path | str = DEFAULT_CACHE_DIR, *, enabled: bool = True):
        self.dir = Path(cache_dir)
        self.enabled = enabled
        self._mem: dict[str, tuple[float, ...]] = {}
        self._lock = threading.Lock()
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.dir / key[:2] / f"{key}.vec"

    def get(self, key: str) -> tuple[float, ...] | None:
        if not self.enabled:
            return None
        with self._lock:
            hit = self._mem.get(key)
        if hit is not None:
            metrics.count_embed_cache_hit()
            return hit
        p = self._path(key)
        if not p.exists():
            return None
        try:
            raw = p.read_bytes()
            n = len(raw) // 4
            vec = tuple(struct.unpack(f"<{n}f", raw))
        except Exception:
            return None
        with self._lock:
            self._mem[key] = vec
        metrics.count_embed_cache_hit()
        return vec

    def put(self, key: str, vec: tuple[float, ...]) -> None:
        with self._lock:
            self._mem[key] = vec
        if not self.enabled:
            return
        p = self._path(key)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(struct.pack(f"<{len(vec)}f", *vec))
        except Exception:
            pass  # cache failures must never break a write path

    def stats(self) -> dict:
        with self._lock:
            return {"in_memory": len(self._mem), "dir": str(self.dir),
                    "enabled": self.enabled}
