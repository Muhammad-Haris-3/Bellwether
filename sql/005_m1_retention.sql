-- Bellwether — 005 M1: sealing and retention (M1-FR-8 to FR-11)
--
-- The problem this file solves: bellwether_writer is DENIED delete by grant,
-- and that denial is the append-only guarantee. Retention needs to delete.
--
-- The wrong fixes are to give the writer DELETE (which voids the guarantee) or
-- to put the owner credential in GitHub Actions (which is worse — it voids
-- everything). The fix used here is a SECURITY DEFINER function owned by the
-- database owner: the writer still cannot delete a single row of its choosing,
-- but it may invoke a routine that deletes rows meeting a condition the writer
-- does not control and cannot pass in.
--
-- The age floors are enforced INSIDE the function. A caller may ask for a more
-- conservative cutoff than the floor; it cannot ask for a more aggressive one.

-- ---------------------------------------------------------------------------
-- outcome.seals
--
-- One row per sealed month. The digest is computed in Python over the rows in
-- a fixed order (bellwether/seal.py) and the same file is committed to the
-- repository as seals/YYYY-MM.json.
--
-- That pairing is the point: rows can age out of a 0.5 GB database while the
-- proof they were not altered persists in public git history, verifiable by
-- someone with access to neither the database nor the author. SRS 6.5 promised
-- indefinite retention of evidence; at this budget that promise cannot be
-- kept, and replacing it with a checkable one is better than quietly breaking
-- it (M1 section 6).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outcome.seals (
    month           date        PRIMARY KEY,
    sealed_at_utc   timestamptz NOT NULL DEFAULT now(),
    row_counts      jsonb       NOT NULL,
    digest          text        NOT NULL,
    algorithm       text        NOT NULL DEFAULT 'sha256',
    sealed_by_run   uuid
);

-- A seal is evidence about evidence. Nothing may rewrite one.
REVOKE UPDATE, DELETE, TRUNCATE ON outcome.seals FROM bellwether_writer;
GRANT  SELECT, INSERT            ON outcome.seals TO   bellwether_writer;
GRANT  SELECT                    ON outcome.seals TO   bellwether_readonly;


-- ---------------------------------------------------------------------------
-- Cohort flag on label_checks
--
-- Cohort checks are kept six months and the rest thirty days, and the flag
-- lives on rc_events — which is pruned at thirty days. Resolving the split by
-- joining rc_events would therefore stop working exactly when it starts to
-- matter. One boolean, denormalised on purpose.
-- ---------------------------------------------------------------------------
ALTER TABLE outcome.label_checks
    ADD COLUMN IF NOT EXISTS in_maturity_cohort boolean NOT NULL DEFAULT false;

UPDATE outcome.label_checks c
   SET in_maturity_cohort = e.in_maturity_cohort
  FROM landing.rc_events e
 WHERE e.revid = c.revid
   AND c.in_maturity_cohort IS DISTINCT FROM e.in_maturity_cohort;


-- ---------------------------------------------------------------------------
-- landing.prune_expired
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION landing.prune_expired(
    p_dry_run        boolean DEFAULT true,
    p_raw_days       integer DEFAULT 30,
    p_evidence_days  integer DEFAULT 90,
    p_cohort_days    integer DEFAULT 180
)
RETURNS TABLE (target text, rows_affected bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, landing, outcome
AS $$
DECLARE
    v_raw      integer := GREATEST(p_raw_days, 7);
    v_evidence integer := GREATEST(p_evidence_days, 30);
    v_cohort   integer := GREATEST(p_cohort_days, 90);
    v_count    bigint;
BEGIN
    -- landing.rc_events — raw material, not evidence, and regenerable in
    -- principle from the source while it remains within the wiki's own horizon.
    IF p_dry_run THEN
        SELECT count(*) INTO v_count FROM landing.rc_events
         WHERE event_ts < now() - make_interval(days => v_raw);
    ELSE
        WITH gone AS (
            DELETE FROM landing.rc_events
             WHERE event_ts < now() - make_interval(days => v_raw)
            RETURNING 1)
        SELECT count(*) INTO v_count FROM gone;
    END IF;
    target := 'landing.rc_events'; rows_affected := v_count; RETURN NEXT;

    -- outcome.label_checks — the non-cohort majority ages with the raw data;
    -- the cohort is the survival study and is kept far longer.
    IF p_dry_run THEN
        SELECT count(*) INTO v_count FROM outcome.label_checks
         WHERE (NOT in_maturity_cohort AND checked_at_utc < now() - make_interval(days => v_raw))
            OR (in_maturity_cohort AND checked_at_utc < now() - make_interval(days => v_cohort));
    ELSE
        WITH gone AS (
            DELETE FROM outcome.label_checks
             WHERE (NOT in_maturity_cohort
                    AND checked_at_utc < now() - make_interval(days => v_raw))
                OR (in_maturity_cohort
                    AND checked_at_utc < now() - make_interval(days => v_cohort))
            RETURNING 1)
        SELECT count(*) INTO v_count FROM gone;
    END IF;
    target := 'outcome.label_checks'; rows_affected := v_count; RETURN NEXT;

    -- outcome.labels — evidence. Deleted ONLY from months that have been
    -- sealed (M1-FR-9). An unsealed month is unprunable by construction, so
    -- the seal cannot be forgotten: forgetting it stops the pruning instead of
    -- silently destroying the thing the seal was supposed to attest to.
    IF p_dry_run THEN
        SELECT count(*) INTO v_count FROM outcome.labels l
         WHERE l.first_observed_at_utc < now() - make_interval(days => v_evidence)
           AND EXISTS (SELECT 1 FROM outcome.seals s
                        WHERE s.month = date_trunc('month', l.first_observed_at_utc)::date);
    ELSE
        WITH gone AS (
            DELETE FROM outcome.labels l
             WHERE l.first_observed_at_utc < now() - make_interval(days => v_evidence)
               AND EXISTS (SELECT 1 FROM outcome.seals s
                            WHERE s.month = date_trunc('month', l.first_observed_at_utc)::date)
            RETURNING 1)
        SELECT count(*) INTO v_count FROM gone;
    END IF;
    target := 'outcome.labels'; rows_affected := v_count; RETURN NEXT;

    RETURN;
END;
$$;

-- The writer may CALL it. It still cannot DELETE anything itself.
REVOKE ALL   ON FUNCTION landing.prune_expired(boolean, integer, integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION landing.prune_expired(boolean, integer, integer, integer)
    TO bellwether_writer;
