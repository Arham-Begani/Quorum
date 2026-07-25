-- Agent registry for the Atlas Travel swarm. Authority tiers per CLAUDE.md §5:
-- lower tier number == more authoritative.
--
--   1  booking_agent, confirmation_agent  -- confirmed external facts
--   2  policy_agent, budget_agent         -- constraints
--   3  flight_agent, lodging_agent, ground_agent -- plans and proposals
--   4  research_agent                     -- inferences
--
-- The tier is what rule R1 arbitrates on, so an unattributed or unregistered
-- writer is unresolvable by the policy engine. [I6]

UPSERT INTO agent_registry (agent_id, role, authority_tier, visibility_scopes) VALUES
  ('booking-1',      'booking_agent',      1, ARRAY['workspace']),
  ('confirmation-1', 'confirmation_agent', 1, ARRAY['workspace']),
  ('policy-1',       'policy_agent',       2, ARRAY['workspace']),
  ('budget-1',       'budget_agent',       2, ARRAY['workspace']),
  ('flight-1',       'flight_agent',       3, ARRAY['workspace']),
  ('lodging-1',      'lodging_agent',      3, ARRAY['workspace']),
  ('ground-1',       'ground_agent',       3, ARRAY['workspace']),
  ('ground-2',       'ground_agent',       3, ARRAY['workspace']),
  ('research-1',     'research_agent',     4, ARRAY['workspace']);
