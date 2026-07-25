-- Garbage-collection TTL for the AS OF SYSTEM TIME forensic view.
--
-- The default GC window is far too short for a demo recorded days after a run:
-- AS OF SYSTEM TIME can only read inside the GC window, so a short TTL silently
-- kills the forensic timeline. 90000s is ~25 hours. Set this at provisioning
-- time, before anything writes data. (CLAUDE.md §15.1)
--
-- Verify with a timestamp older than the table itself, e.g. the day after:
--   SELECT count(*) FROM memory_atom AS OF SYSTEM TIME '-24h';

ALTER TABLE memory_atom     CONFIGURE ZONE USING gc.ttlseconds = 90000;
ALTER TABLE memory_conflict CONFIGURE ZONE USING gc.ttlseconds = 90000;
ALTER TABLE action_log      CONFIGURE ZONE USING gc.ttlseconds = 90000;
ALTER TABLE run             CONFIGURE ZONE USING gc.ttlseconds = 90000;
