-- 025 — add expired-session pruning to the maintenance function (M6-FR-36).
--
-- Migration 024 granted bellwether_writer DELETE on app.sessions together with
-- an RLS policy that confines it to expired rows. That grant exists for direct
-- pruning. This migration routes session pruning through landing.prune_expired
-- instead, so it participates in the same dry-run / apply cycle, is reported
-- in the same job output, and is guarded by the same advisory lock.
--
-- The function is SECURITY DEFINER (defined by the database owner) so it
-- bypasses the FORCE RLS on app.sessions. The WHERE clause enforces the same
-- condition the policy would, so the outcome is identical to what the policy
-- would allow — the policy remains as a second guard for the direct-DELETE path.

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

    -- register.predictions — evidence, sealed before pruning like labels.
    --
    -- Carried forward from sql/011, which EXTENDED this function after sql/005
    -- first defined it. The first draft of this migration was written from
    -- 005's body and CREATE OR REPLACE silently dropped this limb: predictions
    -- would never have been pruned again, and their seal guard would have
    -- disappeared with them. tests/test_register.py caught it.
    --
    -- The lesson is about the mechanism rather than the omission. Replacing a
    -- function that a later migration extended reverts that extension without
    -- a word, so the replacement has to be a superset of every version before
    -- it and not merely of the one it was copied from.
    IF p_dry_run THEN
        SELECT count(*) INTO v_count FROM register.predictions p
         WHERE p.scored_at < now() - make_interval(days => v_evidence)
           AND EXISTS (SELECT 1 FROM outcome.seals s
                        WHERE s.month = date_trunc('month', p.scored_at)::date);
    ELSE
        WITH gone AS (
            DELETE FROM register.predictions p
             WHERE p.scored_at < now() - make_interval(days => v_evidence)
               AND EXISTS (SELECT 1 FROM outcome.seals s
                            WHERE s.month = date_trunc('month', p.scored_at)::date)
            RETURNING 1)
        SELECT count(*) INTO v_count FROM gone;
    END IF;
    target := 'register.predictions'; rows_affected := v_count; RETURN NEXT;

    -- app.sessions — credentials with an expiry, not evidence. Expired sessions
    -- accumulate silently: they can never be used again but they still occupy
    -- rows. The RLS policy on this table enforces the same predicate, so the
    -- direct-DELETE path (migration 024) is also safe; both paths reach the same
    -- set of rows.
    IF p_dry_run THEN
        SELECT count(*) INTO v_count FROM app.sessions
         WHERE expires_at < now();
    ELSE
        WITH gone AS (
            DELETE FROM app.sessions
             WHERE expires_at < now()
            RETURNING 1)
        SELECT count(*) INTO v_count FROM gone;
    END IF;
    target := 'app.sessions'; rows_affected := v_count; RETURN NEXT;

    RETURN;
END;
$$;

-- Grants are unchanged from 005; this statement is safe to repeat.
REVOKE ALL    ON FUNCTION landing.prune_expired(boolean, integer, integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION landing.prune_expired(boolean, integer, integer, integer)
    TO bellwether_writer;
