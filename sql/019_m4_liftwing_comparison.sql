-- 019 — the institutional benchmark, stored beside the metric it qualifies
-- (M4-FR-18, M4-FR-19).
--
-- Wikimedia runs revertrisk-language-agnostic in production. SRS §6.4 recorded,
-- before any model existed, that it is expected to win. This is where that
-- prediction gets settled.
--
-- Held on the same row as the model's own figure rather than in a table of its
-- own, because the only meaningful comparison is PAIRED — the same events, the
-- same maturity window, the same exclusions. A separate table would make it
-- possible to publish one number against a different population and never
-- notice.
ALTER TABLE outcome.prediction_metrics
    -- How many of the n in this row carried a Lift Wing score. Lift Wing is
    -- SAMPLED, not exhaustive (M4-FR-25), so this is always smaller than n and
    -- the comparison is computed on the paired subset alone. Publishing the
    -- margin without this would invite reading it as covering the whole window.
    ADD COLUMN IF NOT EXISTS liftwing_n integer NOT NULL DEFAULT 0;

ALTER TABLE outcome.prediction_metrics
    ADD COLUMN IF NOT EXISTS liftwing_pr_auc double precision;

-- Positive means Bellwether ahead. The sign is stated here because a margin
-- whose direction is inferred from context is a margin that will eventually be
-- quoted backwards.
ALTER TABLE outcome.prediction_metrics
    ADD COLUMN IF NOT EXISTS liftwing_margin double precision;

ALTER TABLE outcome.prediction_metrics
    ADD COLUMN IF NOT EXISTS liftwing_margin_ci_low double precision;

ALTER TABLE outcome.prediction_metrics
    ADD COLUMN IF NOT EXISTS liftwing_margin_ci_high double precision;

-- Bellwether's own PR-AUC over exactly the paired subset, which is NOT the
-- pr_auc column. That one covers the whole window; this one covers the events
-- Lift Wing also scored. Comparing the full-window figure against the paired
-- one would be a comparison of two different populations dressed as a margin.
ALTER TABLE outcome.prediction_metrics
    ADD COLUMN IF NOT EXISTS model_pr_auc_on_paired double precision;

-- M4-FR-21. If the service is gated or unavailable, that is recorded as an
-- unmet dependency rather than worked around by substituting a comparator that
-- happens to be reachable.
CREATE TABLE IF NOT EXISTS outcome.liftwing_attempts (
    attempt_id     bigserial   PRIMARY KEY,
    attempted_at   timestamptz NOT NULL DEFAULT now(),
    requested      integer     NOT NULL CHECK (requested >= 0),
    fetched        integer     NOT NULL CHECK (fetched >= 0),
    status         text        NOT NULL
        CHECK (status IN ('ok', 'gated', 'unavailable', 'partial')),
    detail         text,
    run_id         uuid
);

CREATE INDEX IF NOT EXISTS liftwing_attempts_recent_idx
    ON outcome.liftwing_attempts (attempted_at DESC);

REVOKE UPDATE, DELETE, TRUNCATE ON outcome.liftwing_attempts FROM bellwether_writer;
GRANT  SELECT, INSERT            ON outcome.liftwing_attempts TO   bellwether_writer;
GRANT  SELECT                    ON outcome.liftwing_attempts TO   bellwether_readonly;
GRANT USAGE, SELECT ON SEQUENCE outcome.liftwing_attempts_attempt_id_seq TO bellwether_writer;
