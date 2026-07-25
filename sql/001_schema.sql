-- Quorum schema. Authoritative DDL. See CLAUDE.md §5 for field semantics.
-- Apply with: python -m quorum.db.migrate
--
-- Statements are applied one at a time by the migration runner, because
-- CREATE DATABASE cannot run inside a multi-statement transaction in
-- CockroachDB.

CREATE DATABASE IF NOT EXISTS quorum;

-- memory_atom -- the unit of memory. One immutable claim with attribution and
-- a validity interval. Append-only: nothing is UPDATEd in place except
-- valid_to, superseded_by, status, evidence_count and confidence. [I4]
CREATE TABLE IF NOT EXISTS memory_atom (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id    UUID        NOT NULL,
  subject_key     STRING      NOT NULL,
  predicate       STRING      NOT NULL,
  object_text     STRING      NOT NULL,
  object_json     JSONB,
  embedding       VECTOR(1024) NOT NULL,
  writer_agent_id STRING      NOT NULL,
  writer_role     STRING      NOT NULL,
  confidence      FLOAT       NOT NULL DEFAULT 0.5,
  evidence_count  INT         NOT NULL DEFAULT 1,
  valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
  valid_to        TIMESTAMPTZ,
  superseded_by   UUID,
  status          STRING      NOT NULL DEFAULT 'active',
  visibility      STRING      NOT NULL DEFAULT 'workspace',
  run_id          UUID,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_status CHECK (status IN
    ('active','superseded','contested','rejected')),
  CONSTRAINT ck_visibility CHECK (visibility IN
    ('workspace','role','private')),
  CONSTRAINT ck_conf CHECK (confidence BETWEEN 0 AND 1)
);

-- memory_conflict -- every detection, benign or not. The ratio of benign to
-- contradictory detections is itself a credibility signal, so we write a row
-- for agreement and unrelated verdicts too.
CREATE TABLE IF NOT EXISTS memory_conflict (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id      UUID        NOT NULL,
  run_id            UUID,                    -- nullable: see 005
  incoming_atom_id  UUID,
  existing_atom_id  UUID        NOT NULL,
  subject_key       STRING      NOT NULL,
  detector          STRING      NOT NULL,   -- tier1_structural | tier2_semantic
  similarity        FLOAT,
  verdict           STRING      NOT NULL,   -- agreement|refinement|contradiction|unrelated
  resolution        STRING      NOT NULL,   -- accept|supersede|reinforce|reject|contest
  policy_rule       STRING,                 -- R1|R2|R3|R4|refinement|agreement|unrelated
  rationale         STRING,
  adjudicator_ms    INT,
  detected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_detector CHECK (detector IN ('tier1_structural','tier2_semantic')),
  CONSTRAINT ck_verdict CHECK (verdict IN
    ('agreement','refinement','contradiction','unrelated')),
  CONSTRAINT ck_resolution CHECK (resolution IN
    ('accept','supersede','reinforce','reject','contest'))
);
