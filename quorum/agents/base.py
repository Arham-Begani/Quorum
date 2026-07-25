"""Agent scaffold.

Five specialists share ONE implementation, parameterised by identity. All
memory access goes through the injected MemoryClient; no agent ever touches the
database directly. Every tool with an external effect routes through
`memory.act()` with its `required_keys` declared, which is what makes the
action gate real rather than decorative. (BUILD.md §6.2)

Turn planning is DETERMINISTIC by design. The canonical scenarios must
reproduce their contradictions on every run [I9], and a scenario that only
sometimes shows the bug is not a demo. Agent turns therefore come from the
scenario plan rather than from sampling a model. An LLM-planned mode is a
natural extension once Bedrock credentials exist, but it would be layered on
top of this, not replace it -- the memory-consistency claim is about what the
memory layer does with claims, not about how the claims were authored.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from ..memory.base import MemoryClient
from ..memory.schema import Action, AgentCtx, Claim, GateResult, RememberResult
from ..policy.tiers import tier_of


@dataclass(frozen=True)
class Agent:
    agent_id: str
    role: str
    memory: MemoryClient
    workspace_id: uuid.UUID

    @property
    def ctx(self) -> AgentCtx:
        return AgentCtx(self.agent_id, self.role, tier_of(self.role))

    @property
    def authority_tier(self) -> int:
        return tier_of(self.role)

    # -- memory ---------------------------------------------------------
    def remember(self, subject_key: str, predicate: str, object_text: str,
                 object_json: dict | None, confidence: float = 0.6,
                 visibility: str = "workspace") -> RememberResult:
        return self.memory.remember(Claim(
            workspace_id=self.workspace_id, subject_key=subject_key,
            predicate=predicate, object_text=object_text, object_json=object_json,
            agent_id=self.agent_id, role=self.role, confidence=confidence,
            visibility=visibility,
        ))

    def recall(self, query: str, subject_keys: list[str] | None = None):
        return self.memory.recall(query, agent=self.ctx,
                                  workspace_id=self.workspace_id,
                                  subject_keys=subject_keys)

    # -- effects --------------------------------------------------------
    def act(self, action_type: str, payload: dict,
            required_keys: tuple[str, ...]) -> GateResult:
        """Every externally-visible effect goes through the gate."""
        return self.memory.act(Action(
            workspace_id=self.workspace_id, agent_id=self.agent_id,
            action_type=action_type, payload=payload, required_keys=required_keys,
        ))


def build_swarm(memory: MemoryClient, workspace_id: uuid.UUID) -> dict[str, Agent]:
    """The Atlas Travel concierge swarm, keyed by agent_id."""
    roster = [
        ("booking-1", "booking_agent"),
        ("confirmation-1", "confirmation_agent"),
        ("policy-1", "policy_agent"),
        ("budget-1", "budget_agent"),
        ("flight-1", "flight_agent"),
        ("lodging-1", "lodging_agent"),
        ("ground-1", "ground_agent"),
        ("ground-2", "ground_agent"),
        ("research-1", "research_agent"),
    ]
    return {aid: Agent(aid, role, memory, workspace_id) for aid, role in roster}
