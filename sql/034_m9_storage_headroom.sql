-- Bellwether — 034 M9: storage headroom
--
-- Idempotent.
--
-- WHY THIS EXISTS
--
-- On 2026-09-10 Neon refused every write: "could not extend file because
-- project size limit (512 MB) has been exceeded". Ingestion, scoring and revert
-- folding all died on their first INSERT, which is why the status page showed
-- score and apply_reverts failed with nothing wrong in either.
--
-- The retention job had warned for five days — in a log line — and measured two
-- schemas of six, so it reported 92% of a 400 MB budget while the project stood
-- at 100% of the 512 MB cap. Neon counts every database on the project, the
-- system ones included (~23 MB). bellwether/retention.py now measures that.
--
-- Three remedies here. The evidence window is in config.py.
--
-- 1. INDEXES NOBODY READS. Each was never scanned, or duplicates the leading
--    column of a unique index that already serves the same lookup. Dropping an
--    index is the only way to get space back without rewriting a table, and a
--    rewrite needs exactly the free space a full database does not have.
--
--      register.predictions  predictions_role_model_idx  29 MB  39 scans a month;
--                            promote's join is served by predictions_once_per_model
--      outcome.label_checks  label_checks_revid_idx      10 MB  duplicates
--                            label_checks_one_per_checkpoint (revid, ...)
--      outcome.labels        labels_revid_idx             9 MB  duplicates
--                            labels_one_per_source (revid, ...)
--      outcome.revert_events revert_events_user_idx       3 MB  never scanned
--      landing.rc_events     rc_events_tag_ids_gin_idx    2 MB  never scanned
--
--    Their CREATE statements are removed from the migrations that introduced
--    them. Left in, every bootstrap would rebuild 53 MB for this file to drop,
--    and near the cap the rebuild is what would fail.
--
-- 2. FREED SPACE THAT WAITS TOO LONG. Retention deletes about a thirtieth of each
--    pruned table a day. At the default 20% threshold autovacuum leaves six days
--    of deleted rows unreclaimed, and the table grows into the gap instead of
--    reusing it. At 2% the space is reusable the day it is freed.
--
-- 3. BOOKKEEPING THAT IS NEVER PRUNED. landing.prune_bookkeeping, below.
--
-- 4. A FOREIGN KEY THAT WOULD HAVE STOPPED RETENTION OUTRIGHT. See the end of
--    this file.
--
-- None of this shrinks a file. Deleted space is reused, not returned, so this
-- stops the growth; it does not undo it.

DROP INDEX IF EXISTS register.predictions_role_model_idx;
DROP INDEX IF EXISTS outcome.label_checks_revid_idx;
DROP INDEX IF EXISTS outcome.labels_revid_idx;
DROP INDEX IF EXISTS outcome.revert_events_user_idx;
DROP INDEX IF EXISTS landing.rc_events_tag_ids_gin_idx;

ALTER TABLE landing.rc_events              SET (autovacuum_vacuum_scale_factor = 0.02);
ALTER TABLE outcome.label_checks           SET (autovacuum_vacuum_scale_factor = 0.02);
ALTER TABLE outcome.labels                 SET (autovacuum_vacuum_scale_factor = 0.02);
ALTER TABLE register.predictions           SET (autovacuum_vacuum_scale_factor = 0.02);
ALTER TABLE outcome.revert_events          SET (autovacuum_vacuum_scale_factor = 0.02);
ALTER TABLE landing.state_applied_events   SET (autovacuum_vacuum_scale_factor = 0.02);
ALTER TABLE landing.state_applied_reverts  SET (autovacuum_vacuum_scale_factor = 0.02);

-- Rows that describe an edit, kept after the edit itself is gone.
--
-- state_applied_events and state_applied_reverts are ledgers against double
-- counting: they stop an edit, or its revert, being folded into the counters
-- twice. revert_events is every revert on the feed. All three were written on
-- every run and pruned by nothing, at ~1.5 MB a day between them.
--
-- A separate function rather than a new limb on prune_expired: replacing that
-- one has already once dropped a limb a later migration added (see sql/025), and
-- nothing here needs its evidence rules.
--
-- Safe past the raw horizon plus a week. Each row is timestamped no earlier than
-- the edit it concerns, so past this line that edit is already out of
-- landing.rc_events, and Wikipedia purges recentchanges at thirty days, so
-- nothing can bring it back to be folded a second time. apply_reverts only
-- consults ledger rows for edits inside its own thirty-day window. The week is
-- margin, not arithmetic.
CREATE OR REPLACE FUNCTION landing.prune_bookkeeping(
    p_dry_run  boolean DEFAULT true,
    p_raw_days integer DEFAULT 30
)
RETURNS TABLE (target text, rows_affected bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, landing, outcome
AS $$
DECLARE
    v_cutoff timestamptz := now() - make_interval(days => GREATEST(p_raw_days, 7) + 7);
    v_table  text;
    v_column text;
    v_count  bigint;
BEGIN
    -- Constants, never caller input: the caller chooses only how old.
    FOR v_table, v_column IN VALUES
        ('landing.state_applied_events',  'applied_at_utc'),
        ('landing.state_applied_reverts', 'applied_at_utc'),
        ('outcome.revert_events',         'revert_ts')
    LOOP
        IF p_dry_run THEN
            EXECUTE format('SELECT count(*) FROM %s WHERE %I < $1', v_table, v_column)
               INTO v_count USING v_cutoff;
        ELSE
            EXECUTE format(
                'WITH gone AS (DELETE FROM %s WHERE %I < $1 RETURNING 1) '
                'SELECT count(*) FROM gone', v_table, v_column)
               INTO v_count USING v_cutoff;
        END IF;
        target := v_table; rows_affected := v_count; RETURN NEXT;
    END LOOP;
END;
$$;
REVOKE ALL    ON FUNCTION landing.prune_bookkeeping(boolean, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION landing.prune_bookkeeping(boolean, integer) TO bellwether_writer;

-- Reviewer verdicts outlive the edit they judged, exactly as labels do.
--
-- sql/003 dropped the foreign keys from labels and label_checks onto rc_events
-- for M1-FR-10: evidence is kept longer than raw material, and a foreign key
-- chains the longer retention to the shorter one. sql/022 then added
-- app.human_labels with the same foreign key and nothing dropped it.
--
-- It had not bitten only because no reviewed edit was thirty days old yet. The
-- first one would have failed prune_expired's DELETE on rc_events as a whole —
-- one judged edit blocking the retention of every other — and the database
-- would have filled again with retention reporting errors instead of space.
--
-- The same argument makes this safe: revid is MediaWiki's immutable identifier
-- and means the same thing with no local row to point at.
ALTER TABLE app.human_labels DROP CONSTRAINT IF EXISTS human_labels_revid_fkey;
