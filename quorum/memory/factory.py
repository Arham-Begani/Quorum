"""THE ONLY place in the codebase that branches on mode. [I8]

The three modes share one workload driver, one seed, one agent implementation.
The mode is injected as a MemoryClient implementation and nothing else. If an
`if mode ==` appears anywhere else, the comparison has stopped being honest and
the demo is no longer evidence of anything.

Enforced by tools/lint_modes.py in CI.
"""

from __future__ import annotations

from .base import MemoryClient
from .naive import NaiveMemory
from .quorum import QuorumMemory
from .txn_only import TxnOnlyMemory

MODES = ("naive", "txn_only", "quorum")

_IMPLEMENTATIONS = {
    "naive": NaiveMemory,
    "txn_only": TxnOnlyMemory,
    "quorum": QuorumMemory,
}


def make_memory(mode: str, pool, embedder, cfg=None) -> MemoryClient:
    try:
        impl = _IMPLEMENTATIONS[mode]
    except KeyError:
        raise ValueError(
            f"unknown mode {mode!r}; expected one of {', '.join(MODES)}"
        ) from None
    return impl(pool, embedder, cfg)
