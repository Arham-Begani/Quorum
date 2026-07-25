"""Mode `naive` — the honest baseline. Separate stores, no transaction, no policy.

This must be what a competent engineer would ACTUALLY build on a vector store,
not a strawman with deliberate bugs. If the baseline is unfair the whole
comparison is worthless and a sharp judge will catch it. (CLAUDE.md §15.6)

So what does that engineer build?

  * embed the claim, ANN-search for similar memories
  * if something near-identical is already there, treat it as a duplicate and
    skip -- this is the dedup everyone writes
  * otherwise append
  * read and write are SEPARATE operations, each autocommitted

What they do NOT build, because it is the novel part of this project, is an
authority-tier policy engine, supersession, or a contested state. A vector
memory store has no notion of which agent outranks which. So two agents
asserting different check-in dates produce two rows that are both "currently
true", and nothing in the stack objects.

Two independent failure modes follow, and the harness distinguishes them:

  1. SEMANTIC   -- different values are not duplicates, so both get appended.
                   Happens even with no concurrency at all.
  2. RACE       -- the read and the write are not atomic, so even the dedup
                   check misses a writer that lands in between.

Note on fairness: this mode's vector index and its rows are the same
CockroachDB database, so it does not even pay the cross-store replication lag a
real Pinecone+Postgres deployment would. The baseline is given the best case it
could possibly have.
"""

from __future__ import annotations

import time
import uuid

from ..detect.coerce import values_equal
from ..detect.tier1 import scalar_of
from .base import INSERT_ATOM_SQL, MemoryClient, vector_literal
from .schema import Claim, RememberResult, Resolution, Status


class NaiveMemory(MemoryClient):
    mode = "naive"
    uses_semantic_layer = False
    uses_transactions = False
    has_action_gate = False

    def remember(self, claim: Claim) -> RememberResult:
        t0 = time.perf_counter()
        try:
            vec = vector_literal(self.embedder.embed(claim.embed_text()))
        except Exception as exc:
            return RememberResult(None, "error", latency_ms=_ms(t0), error=repr(exc))

        atom_id = uuid.uuid4()
        try:
            with self.pool.connection() as conn:
                conn.autocommit = True          # every statement its own txn
                with conn.cursor() as cur:
                    # --- operation 1: read the neighbourhood -------------
                    neighbours = self._neighbourhood(cur, claim, vec)

                    # The race window a real system has between its vector
                    # search and its write. Widened only when explicitly
                    # configured, and disclosed in the run report.
                    delay_ms = float(self.cfg.get("race_delay_ms", 0))
                    if delay_ms:
                        time.sleep(delay_ms / 1000.0)

                    duplicate = self._find_duplicate(claim, neighbours)
                    if duplicate is not None:
                        return RememberResult(
                            duplicate.id, Resolution.ACCEPT, "naive_dedup",
                            latency_ms=_ms(t0))

                    # --- operation 2: write ------------------------------
                    cur.execute(INSERT_ATOM_SQL,
                                self._insert_atom_params(claim, atom_id, vec,
                                                         status=Status.ACTIVE))
        except Exception as exc:
            return RememberResult(None, "error", latency_ms=_ms(t0), error=repr(exc))

        return RememberResult(atom_id, Resolution.ACCEPT, "naive_append",
                              latency_ms=_ms(t0))

    @staticmethod
    def _find_duplicate(claim: Claim, neighbours):
        """Exact-value dedup on the same attribute. No semantics beyond equality."""
        for atom in neighbours:
            if atom.subject_key != claim.subject_key:
                continue
            if atom.predicate != claim.predicate:
                continue
            a, b = scalar_of(claim.object_json), scalar_of(atom.object_json)
            if a is not None and b is not None and values_equal(a, b):
                return atom
            if claim.object_json is not None and claim.object_json == atom.object_json:
                return atom
        return None


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0
