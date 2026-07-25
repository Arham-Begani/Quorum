-- run_id is harness metadata, not a memory invariant.
--
-- The memory client must be usable outside a scenario run -- in production
-- there is no "run" -- so a NOT NULL run_id makes the library crash in exactly
-- the situation it is meant to serve. Detections and actions are still grouped
-- by run when a run exists; they are simply not required to belong to one.
--
-- Idempotent: DROP NOT NULL on an already-nullable column is a no-op.

ALTER TABLE memory_conflict ALTER COLUMN run_id DROP NOT NULL;
ALTER TABLE action_log      ALTER COLUMN run_id DROP NOT NULL;
