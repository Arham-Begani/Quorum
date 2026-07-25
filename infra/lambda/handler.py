"""Lambda handler — one invocation per agent turn.

The swarm is a fan-out of these. That is what makes the concurrency genuine
rather than simulated with threads, and it is worth saying so explicitly: the
races Quorum defends against are real races between real processes, not a
`threading.Barrier` in a test harness.

NOT DEPLOYED in this submission -- no AWS credentials were configured in the
build environment. The handler is written and the packaging script is here, but
it has not been run on Lambda, and docs/SUBMISSION.md says so.

Event shape:
    {
      "mode": "quorum",
      "run_id": "...", "workspace_id": "...",
      "turn": {"kind": "remember", "agent_id": "lodging-1",
               "subject_key": "...", "predicate": "equals",
               "object_text": "...", "object_json": {...}, "confidence": 0.7}
    }
    {
      "turn": {"kind": "act", "agent_id": "lodging-1",
               "action_type": "book_hotel", "payload": {...},
               "required_keys": ["..."]}
    }
"""

from __future__ import annotations

import json
import os
import uuid

from quorum.agents.base import Agent
from quorum.db.metrics import metrics
from quorum.db.pool import crdb_url, make_pool, quorum_dbname
from quorum.embed.bedrock import Embedder
from quorum.memory.factory import make_memory

# Cold-start once, reuse across invocations. The pool is small because Lambda
# concurrency multiplies it -- a large pool per container is how you exhaust
# the cluster's connection limit under fan-out.
_pool = None
_embedder = None


def _resources():
    global _pool, _embedder
    if _pool is None:
        _pool = make_pool(crdb_url(), min_size=1, max_size=int(
            os.environ.get("LAMBDA_POOL_MAX", 2)),
            dbname=quorum_dbname(), app_name="quorum-lambda")
    if _embedder is None:
        _embedder = Embedder()
    return _pool, _embedder


def handler(event, context):
    pool, embedder = _resources()
    metrics.reset()

    mode = event.get("mode", "quorum")
    run_id = uuid.UUID(event["run_id"]) if event.get("run_id") else None
    workspace_id = uuid.UUID(event["workspace_id"])
    turn = event["turn"]

    memory = make_memory(mode, pool, embedder, {"run_id": run_id})
    agent = Agent(turn["agent_id"], turn.get("role") or _role_of(turn["agent_id"]),
                  memory, workspace_id)

    if turn["kind"] == "remember":
        res = agent.remember(
            turn["subject_key"], turn["predicate"], turn["object_text"],
            turn.get("object_json"), confidence=turn.get("confidence", 0.6))
        body = res.to_dict()
    elif turn["kind"] == "act":
        res = agent.act(turn["action_type"], turn.get("payload", {}),
                        tuple(turn["required_keys"]))
        body = res.to_dict()
    else:
        return {"statusCode": 400,
                "body": json.dumps({"error": f"unknown turn kind {turn['kind']!r}"})}

    _emit_cloudwatch(mode)
    return {"statusCode": 200,
            "body": json.dumps({"mode": mode, "result": body,
                                "performance": metrics.snapshot()}, default=str)}


def _role_of(agent_id: str) -> str:
    return agent_id.rsplit("-", 1)[0] + "_agent"
