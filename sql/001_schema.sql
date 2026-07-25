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
