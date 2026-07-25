"""Role -> authority tier. Lower is more authoritative. (CLAUDE.md §5)

  1  booking_agent, confirmation_agent  -- confirmed external facts. A booking
                                           reference is ground truth.
  2  policy_agent, budget_agent         -- constraints. Authoritative over
                                           preferences.
  3  flight_agent, lodging_agent, ground_agent -- plans and proposals.
  4  research_agent                     -- inferences. Lowest authority.

The database is the source of truth (agent_registry); this table is the
fallback so pure policy unit tests need no cluster.
"""

from __future__ import annotations

DEFAULT_TIER = 4

ROLE_TIERS: dict[str, int] = {
    "booking_agent": 1,
    "confirmation_agent": 1,
    "policy_agent": 2,
    "budget_agent": 2,
    "flight_agent": 3,
    "lodging_agent": 3,
    "ground_agent": 3,
    "research_agent": 4,
}


def tier_of(role: str, registry: dict[str, int] | None = None) -> int:
    if registry and role in registry:
        return registry[role]
    return ROLE_TIERS.get(role, DEFAULT_TIER)


def load_registry(cur) -> dict[str, int]:
    """role -> min(authority_tier) from agent_registry."""
    cur.execute("SELECT role, min(authority_tier) FROM agent_registry GROUP BY role")
    return {row[0]: int(row[1]) for row in cur.fetchall()}
