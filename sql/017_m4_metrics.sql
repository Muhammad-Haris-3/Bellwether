-- 017 — continuous evaluation (M4-FR-9, M4-FR-26).
--
-- Metrics on predictions the project actually made, as opposed to the offline
-- backtest in outcome.evaluations. Both are kept and neither replaces the
-- other: they measure different populations, and a project that quoted
-- whichever was higher would be doing the thing this schema exists to prevent.
--
-- Append-only. A run that produced a bad number cannot be re-run away.
CREATE TABLE IF NOT EXISTS outcome.prediction_metrics (
    metric_id         bigserial   PRIMARY KEY,
    computed_at       timestamptz NOT NULL DEFAULT now(),

    -- Which predictions were in scope.
    window_label      text        NOT NULL CHECK (window_label IN ('7d', '30d', 'all')),
    window_start      timestamptz,
    window_end        timestamptz NOT NULL,

    -- 'all' is the aggregate. Segments are diagnosis, never a headline
    -- (M4-FR-14), and every one is written on every run including the ones
    -- that look bad (M4-FR-13).
    segment           text        NOT NULL DEFAULT 'all',
    segment_level     text        NOT NULL DEFAULT 'all',

    -- Maturity, carried on the row rather than assumed by the reader. It is a
    -- 48h placeholder until the cohort ages (M2 C-1/C-2), and a number whose
    -- provisionality lives in a footnote somewhere else is a number that will
    -- be quoted without it.
    maturity_hours    integer     NOT NULL,
    provisional       boolean     NOT NULL DEFAULT true,

    n                 integer     NOT NULL CHECK (n >= 0),
    n_positives       integer     NOT NULL CHECK (n_positives >= 0),
    base_rate         double precision,
    weighted_base_rate double precision,

    -- PR-AUC is primary and was fixed in PREREGISTRATION.md before M0. The
    -- others sit beside it and never instead of it (M4-FR-10).
    pr_auc            double precision,
    pr_auc_ci_low     double precision,
    pr_auc_ci_high    double precision,
    roc_auc           double precision,
    brier             double precision,

    -- The same opponent as KC-2, on the same events, paired (M4-FR-11).
    baseline_pr_auc   double precision,
    margin            double precision,
    margin_ci_low     double precision,
    margin_ci_high    double precision,

    -- M4-FR-2: exclusions are published, never applied silently.
    --
    -- excluded_late is the one that matters. Dropping predictions whose outcome
    -- was already observable is correct, but they are not a random sample —
    -- they concentrate in edits that were reverted fast, so the exclusion
    -- selects on the outcome. Its own base rate is stored beside the metric it
    -- protects (M4-FR-3), because "we excluded 4%" and "we excluded 4% that
    -- were 60% positive" are different statements.
    excluded_immature integer     NOT NULL DEFAULT 0,
    excluded_late     integer     NOT NULL DEFAULT 0,
    excluded_late_base_rate double precision,

    code_commit       text,
    run_id            uuid,

    CONSTRAINT prediction_metrics_positives_fit CHECK (n_positives <= n)
);

CREATE INDEX IF NOT EXISTS prediction_metrics_recent_idx
    ON outcome.prediction_metrics (computed_at DESC, window_label, segment);

-- Reliability, in bins, with n per bin (M4-FR-15).
--
-- A curve published without its bin counts is unreadable: a decile holding four
-- events and a decile holding four thousand look identical on a chart, and the
-- first one is noise.
CREATE TABLE IF NOT EXISTS outcome.calibration_bins (
    bin_id            bigserial   PRIMARY KEY,
    metric_id         bigint      NOT NULL REFERENCES outcome.prediction_metrics (metric_id),
    bin_index         integer     NOT NULL CHECK (bin_index BETWEEN 0 AND 99),
    bin_low           double precision NOT NULL,
    bin_high          double precision NOT NULL,
    n                 integer     NOT NULL CHECK (n >= 0),
    mean_predicted    double precision,

    -- Raw and population-weighted, always (M1-FR-3, M4-FR-17). The frame keeps
    -- 50% of logged-out edits and 3% of registered ones, so the sample's base
    -- rate is around four times the population's. A model calibrated against
    -- the raw frequency is calibrated to a population that does not exist and
    -- overstates risk in production by roughly that factor.
    observed_rate     double precision,
    weighted_observed_rate double precision,

    CONSTRAINT calibration_bins_one_per_metric UNIQUE (metric_id, bin_index)
);

-- The institutional benchmark (M4-FR-18 to FR-21).
--
-- Stored with the revision id and the fetch time so the comparison survives
-- Wikimedia changing their model. A benchmark that cannot be re-derived after
-- the opponent moves is an anecdote with a date on it.
CREATE TABLE IF NOT EXISTS outcome.liftwing_scores (
    revid            bigint      PRIMARY KEY,
    score            double precision NOT NULL CHECK (score BETWEEN 0 AND 1),
    model_name       text        NOT NULL,
    model_version    text,
    fetched_at_utc   timestamptz NOT NULL DEFAULT now(),
    fetched_by_run   uuid
);

CREATE INDEX IF NOT EXISTS liftwing_fetched_idx ON outcome.liftwing_scores (fetched_at_utc);

-- Evidence, like everything else in outcome.
REVOKE UPDATE, DELETE, TRUNCATE ON outcome.prediction_metrics FROM bellwether_writer;
REVOKE UPDATE, DELETE, TRUNCATE ON outcome.calibration_bins   FROM bellwether_writer;
REVOKE UPDATE, DELETE, TRUNCATE ON outcome.liftwing_scores    FROM bellwether_writer;

GRANT SELECT, INSERT ON outcome.prediction_metrics TO bellwether_writer;
GRANT SELECT, INSERT ON outcome.calibration_bins   TO bellwether_writer;
GRANT SELECT, INSERT ON outcome.liftwing_scores    TO bellwether_writer;

GRANT SELECT ON outcome.prediction_metrics TO bellwether_readonly;
GRANT SELECT ON outcome.calibration_bins   TO bellwether_readonly;
GRANT SELECT ON outcome.liftwing_scores    TO bellwether_readonly;

GRANT USAGE, SELECT ON SEQUENCE outcome.prediction_metrics_metric_id_seq TO bellwether_writer;
GRANT USAGE, SELECT ON SEQUENCE outcome.calibration_bins_bin_id_seq      TO bellwether_writer;
