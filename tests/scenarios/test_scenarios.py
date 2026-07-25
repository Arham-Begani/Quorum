"""S1-S5 across all three modes, asserting the DIVERGENCE.

The point is not that quorum works. The point is that quorum works AND the
other two do not, on the same workload, with the same seed and the same agents.
If txn_only ever matches quorum on these scenarios, the whole submission's
central claim has collapsed and this suite must fail loudly. (CLAUDE.md §2)
"""

from __future__ import annotations

import pytest

from quorum.domain.scenarios import catalog
from quorum.domain.scenarios.base import check
from quorum.harness import driver

from ..conftest import needs_db

pytestmark = needs_db

DELAY_MS = 40


@pytest.fixture(scope="module")
def results(pool, embedder, adjudicator):
    """Run every scenario in every mode once; assert against the results."""
    out = {}
    for sid in catalog.SCENARIO_IDS:
        out[sid] = {}
        for mode in ("naive", "txn_only", "quorum"):
            out[sid][mode] = driver.run(
                catalog.get(sid), mode, pool=pool, embedder=embedder,
                adjudicator=adjudicator, cfg={"race_delay_ms": DELAY_MS})
    return out


def _skip_if_untestable(plan, embedder):
    if plan.requires_semantic_embeddings and not embedder.is_semantic:
        pytest.skip(
            f"{plan.id} needs real semantic embeddings: its conflicting claims do "
            "not share a subject_key, so only ANN over a true embedding space can "
            "surface the pair. Install fastembed for a local model, or set AWS "
            "credentials for Bedrock Titan.")


@pytest.mark.parametrize("scenario_id", catalog.SCENARIO_IDS)
@pytest.mark.parametrize("mode", ["naive", "txn_only", "quorum"])
def test_scenario_matches_expectation(results, embedder, scenario_id, mode):
    plan = catalog.get(scenario_id)
    if mode == "quorum":
        _skip_if_untestable(plan, embedder)
    exp = plan.expectations[mode]
    a = results[scenario_id][mode].anomalies

    assert check(exp.contradictory_active_pairs, a["contradictory_active_pairs"]), (
        f"{scenario_id}/{mode}: expected contradictory_active_pairs "
        f"{exp.contradictory_active_pairs}, got {a['contradictory_active_pairs']}. "
        f"{exp.note}")
    assert check(exp.wrong_actions, a["wrong_actions"]), (
        f"{scenario_id}/{mode}: expected wrong_actions {exp.wrong_actions}, "
        f"got {a['wrong_actions']}. {exp.note}")
    assert check(exp.blocked_actions, a["blocked_actions"]), (
        f"{scenario_id}/{mode}: expected blocked_actions {exp.blocked_actions}, "
        f"got {a['blocked_actions']}. {exp.note}")


@pytest.mark.parametrize("scenario_id", catalog.SCENARIO_IDS)
def test_quorum_never_leaves_contradictory_memory(results, embedder, scenario_id):
    """The one invariant that holds across every scenario without exception."""
    _skip_if_untestable(catalog.get(scenario_id), embedder)
    a = results[scenario_id]["quorum"].anomalies
    assert a["contradictory_active_pairs"] == 0, (
        f"{scenario_id}: quorum left contradictory active memory: "
        f"{a['details']['contradictory_pairs']}")


@pytest.mark.parametrize("scenario_id", ["S1_checkin_date", "S3_ground_overlap",
                                         "S4_preference_reversal",
                                         "S5_concurrent_race"])
def test_txn_only_diverges_from_quorum(results, scenario_id):
    """THE pivot. Serializable isolation, used correctly, still ends up wrong.

    If this ever passes trivially -- because txn_only stopped failing -- the
    project no longer demonstrates anything a database does not already do.
    """
    txn = results[scenario_id]["txn_only"].anomalies
    quo = results[scenario_id]["quorum"].anomalies
    assert txn["contradictory_active_pairs"] > 0, (
        f"{scenario_id}: txn_only produced NO contradictory memory. Either the "
        "scenario stopped being a contradiction, or the central claim is wrong.")
    assert quo["contradictory_active_pairs"] == 0
    assert (txn["contradictory_active_pairs"], txn["wrong_actions"]) != \
           (quo["contradictory_active_pairs"], quo["wrong_actions"]), \
        f"{scenario_id}: txn_only and quorum produced identical outcomes"


@pytest.mark.parametrize("scenario_id", catalog.SCENARIO_IDS)
def test_naive_and_txn_only_never_block_anything(results, scenario_id):
    """Neither baseline has an action gate, so nothing is ever refused."""
    for mode in ("naive", "txn_only"):
        a = results[scenario_id][mode].anomalies
        assert a["blocked_actions"] == 0
        assert a["contested_atoms"] == 0


def test_at_least_one_scenario_escalates_to_contest(results, embedder):
    """A system that declines to guess is more convincing than one that always
    has an answer. R4 must be reachable in practice, not just in theory."""
    contested = [sid for sid in catalog.SCENARIO_IDS
                 if results[sid]["quorum"].anomalies["contested_atoms"] > 0]
    assert contested, "no scenario reached CONTEST; R4 is unreachable in practice"


def test_every_detection_is_logged_including_benign(results):
    """The ratio of benign to contradictory detections is a credibility signal."""
    total = sum(results[sid]["quorum"].conflicts["detected"]
                for sid in catalog.SCENARIO_IDS)
    assert total > 0


def test_supersession_is_append_only(results, pool):
    """No DELETE in the memory write path. Ever. [I4]"""
    ws = results["S1_checkin_date"]["quorum"].workspace_id
    with pool.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, valid_to IS NOT NULL, superseded_by IS NOT NULL "
                "FROM memory_atom WHERE workspace_id = %s", (ws,))
            rows = cur.fetchall()
    assert len(rows) >= 2, "the superseded atom was removed instead of closed out"
    superseded = [r for r in rows if r[0] == "superseded"]
    assert superseded, "expected a superseded atom to still be present"
    for _, closed, has_pointer in superseded:
        assert closed and has_pointer, "supersession must set valid_to AND superseded_by"
