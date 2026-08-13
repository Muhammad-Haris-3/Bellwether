-- Bellwether — 011 M3: the prediction register
--
-- Idempotent.
--
-- This is the only artefact in the project that cannot be rebuilt. Features,
-- state, even the raw events can be recomputed or re-fetched from the wiki. A
-- score, once lost or altered, is gone — and with it the only thing that
-- distinguishes a prediction from a description.
--
-- Everything M0 built to guarantee the OUTCOME was honestly observed is worth
-- nothing if the PREDICTION can be edited once the outcome is known. So the
-- protections here are stronger than elsewhere, and they are properties of the
-- database rather than promises about the code.

CREATE SCHEMA IF NOT EXISTS register;


CREATE TABLE IF NOT EXISTS register.predictions (
    prediction_id  bigserial   PRIMARY KEY,
    revid          bigint      NOT NULL,

    -- Denormalised from rc_events on purpose. Raw events are pruned at 30 days
    -- and predictions live for 90, so a join would stop working exactly when
    -- the older predictions are the interesting ones. It also lets the
    -- backdating CHECK below be a CHECK at all — those cannot see other tables.
    event_ts       timestamptz NOT NULL,
    scored_at      timestamptz NOT NULL DEFAULT now(),

    model_version  text        NOT NULL,
    role           text        NOT NULL CHECK (role IN ('champion', 'shadow')),
    score          double precision NOT NULL CHECK (score >= 0.0 AND score <= 1.0),

    -- SRS FR-15. With the model version and the stored state, this is what
    -- makes a historical prediction recomputable rather than merely recorded.
    feature_hash   text        NOT NULL,

    -- M3-FR-10. True when a revert for this edit was ALREADY visible at the
    -- moment we scored it.
    --
    -- If the scorer falls far enough behind — and GitHub's scheduler ran
    -- roughly hourly against a nominal ten minutes in M1 — it will eventually
    -- score an edit that has already been reverted. Nothing raises. The score
    -- is simply trivially correct, and accuracy improves for the worst
    -- possible reason. Flagged here and excluded from every accuracy claim.
    outcome_observable_at_scoring boolean NOT NULL DEFAULT false,

    scored_by_run  uuid,

    -- A backdated score is the simplest way to fake a forecasting record, so
    -- it is made impossible rather than discouraged.
    --
    -- No clock-skew tolerance here, unlike landing.rc_events. There the gap
    -- between an edit and its ingestion can be seconds, so a few seconds of
    -- NTP drift could fail an honest insert. Here the gap is never small: an
    -- edit is ingested before it is scored, and ingestion is itself at least
    -- one polling interval behind. A scored_at earlier than event_ts is not
    -- skew; it is wrong.
    CONSTRAINT predictions_not_backdated CHECK (scored_at >= event_ts),

    -- M3-FR-8. Re-running the scorer must be idempotent by constraint rather
    -- than by the caller remembering. Carrying `role` from the outset means
    -- M5's shadow needs no migration on an evidential table — altering this
    -- one later is exactly what it should be hard to do.
    CONSTRAINT predictions_once_per_model UNIQUE (revid, model_version, role)
);

CREATE INDEX IF NOT EXISTS predictions_event_ts_idx  ON register.predictions (event_ts);
CREATE INDEX IF NOT EXISTS predictions_scored_at_idx ON register.predictions (scored_at);
CREATE INDEX IF NOT EXISTS predictions_role_model_idx
    ON register.predictions (role, model_version, event_ts);


-- ---------------------------------------------------------------------------
-- Grants — the append-only guarantee, as a property
-- ---------------------------------------------------------------------------
GRANT USAGE  ON SCHEMA register TO bellwether_writer, bellwether_readonly;
GRANT SELECT, INSERT ON register.predictions TO bellwether_writer;
GRANT SELECT         ON register.predictions TO bellwether_readonly;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA register TO bellwether_writer;

-- Explicit, not merely withheld. The intent should be visible in the file, and
-- it should survive somebody later adding a careless GRANT ALL above it.
REVOKE UPDATE, DELETE, TRUNCATE ON register.predictions FROM bellwether_writer;

ALTER DEFAULT PRIVILEGES IN SCHEMA register
    GRANT SELECT ON TABLES TO bellwether_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA register
    GRANT SELECT, INSERT ON TABLES TO bellwether_writer;


-- ---------------------------------------------------------------------------
-- Retention (M3-FR-19, M3-FR-20)
--
-- Predictions age out at the evidence retention window, and — like labels —
-- only from a month that has been sealed. Forgetting to seal stops the pruning
-- rather than destroying what the seal was meant to attest to.
--
-- Same signature, so CREATE OR REPLACE keeps the existing grant to the writer.
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
SET search_path = pg_catalog, landing, outcome, register
AS $$
DECLARE
    v_raw      integer := GREATEST(p_raw_days, 7);
    v_evidence integer := GREATEST(p_evidence_days, 30);
    v_cohort   integer := GREATEST(p_cohort_days, 90);
    v_count    bigint;
BEGIN
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

    RETURN;
END;
$$;

REVOKE ALL    ON FUNCTION landing.prune_expired(boolean, integer, integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION landing.prune_expired(boolean, integer, integer, integer)
    TO bellwether_writer;
