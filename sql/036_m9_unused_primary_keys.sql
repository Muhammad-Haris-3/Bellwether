-- Bellwether — 036 M9: primary keys nobody reads
--
-- Idempotent.
--
-- On 2026-10-25 the project stood at 481 MB of Neon's 512 MB cap. Both keys
-- below had zero scans in pg_stat_user_indexes, and nothing in the code reads
-- check_id or label_id. Each row is already identified by its natural unique
-- constraint, which stays:
--
--   outcome.label_checks  label_checks_pkey  13 MB  label_checks_one_per_checkpoint
--   outcome.labels        labels_pkey         9 MB  labels_one_per_source
--
-- Dropping an index returns its files at once; nothing is rewritten. The
-- columns stay, so the sequences and every INSERT are unchanged.

ALTER TABLE outcome.label_checks DROP CONSTRAINT IF EXISTS label_checks_pkey;
ALTER TABLE outcome.labels       DROP CONSTRAINT IF EXISTS labels_pkey;
