-- Indexes. Verified against CockroachDB v26.2.1 by spikes/bootstrap.py.
--
-- CREATE VECTOR INDEX is the syntax that cluster accepts and it produces a
-- C-SPANN index with the vector_l2_ops opclass. Embeddings are unit-normalized
-- (Titan v2 and the spike embedder both return unit vectors), so L2 distance
-- and cosine distance induce the SAME ordering and <-> is the operator to use.
--
-- NOTE ON PREFIX COLUMNS: the neighbourhood query always filters by
-- workspace_id. A plain vector index cannot serve that filter, so at scale the
-- planner falls back to a scan. workspace_id is therefore a prefix column on
-- the vector index. Verified supported on v26.2.1. See
-- docs/CONSISTENCY_MODEL.md for the measured behaviour and its limits.

CREATE VECTOR INDEX IF NOT EXISTS idx_atom_embedding
  ON memory_atom (workspace_id, embedding);

-- The hot structural lookup: exact subject_key within a workspace, live rows
-- only. This is what makes tier-1 detection reliable regardless of ANN recall.
CREATE INDEX IF NOT EXISTS idx_atom_subject_live
  ON memory_atom (workspace_id, subject_key)
  WHERE valid_to IS NULL;

CREATE INDEX IF NOT EXISTS idx_atom_status_live
  ON memory_atom (workspace_id, status)
  WHERE valid_to IS NULL;

CREATE INDEX IF NOT EXISTS idx_atom_run
  ON memory_atom (run_id, created_at);

CREATE INDEX IF NOT EXISTS idx_conflict_run
  ON memory_conflict (run_id, detected_at);

CREATE INDEX IF NOT EXISTS idx_action_run
  ON action_log (run_id, created_at);
