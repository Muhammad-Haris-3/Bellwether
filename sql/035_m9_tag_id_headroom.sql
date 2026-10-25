-- Bellwether — 035 M9: tag id headroom
--
-- Idempotent.
--
-- WHY THIS EXISTS
--
-- From 2026-10-21 every ingest run failed on its first new tag:
-- "nextval: reached maximum value of sequence tag_names_tag_id_seq (32767)".
-- About 70 tags exist. The rest of the range was spent by ensure_tag_ids, which
-- re-inserted every tag on every run with ON CONFLICT DO NOTHING; a skipped
-- insert still draws an id. bellwether/ingest.py now looks up first and only
-- inserts tags it has never seen.
--
-- The ids already handed out stay as they are: rc_events.tag_ids holds them.
-- The negative half of smallint was never used, so the sequence moves there.
-- With the leak closed that is 32,767 genuinely new tags, not 32,767 runs.
-- tag_ids is written, never read as a number, so negative ids change nothing.
--
-- Guarded, because migrate re-runs every file: restarting again after new tags
-- had taken -32768, -32767 ... would hand those ids out a second time.

DO $$
BEGIN
    IF (SELECT seqmin FROM pg_sequence
         WHERE seqrelid = pg_get_serial_sequence('landing.tag_names', 'tag_id')::regclass) > -32768
    THEN
        ALTER TABLE landing.tag_names
            ALTER COLUMN tag_id SET MINVALUE -32768,
            ALTER COLUMN tag_id RESTART WITH -32768;
    END IF;
END
$$;
