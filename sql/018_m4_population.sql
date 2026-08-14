-- 018 — which population a metric describes (M4 §3).
--
-- Two populations are now graded on every run, and they are not interchangeable:
--
--   all              every scored event, matured at seven days, because outside
--                    the maturity cohort the labeller checks exactly once and it
--                    does so at the seven-day final checkpoint
--   maturity_cohort  the 10% that receives the full checkpoint grid, matured at
--                    48 hours, because a 48h check exists for every one of them
--
-- The cohort figure arrives five days sooner and describes a tenth as many
-- events. Both are true; neither is the other. Kept in one table with a column
-- naming which, rather than in two tables that would drift, and NEVER collapsed
-- into a single headline — that is precisely the substitution a reader would
-- make silently if the column did not exist.
--
-- The cohort is a deterministic 10% bucket of the sampling frame, so it is a
-- probability sample of the frame and unbiased for it. Smaller, not skewed.
ALTER TABLE outcome.prediction_metrics
    ADD COLUMN IF NOT EXISTS population text NOT NULL DEFAULT 'all';

ALTER TABLE outcome.prediction_metrics
    DROP CONSTRAINT IF EXISTS prediction_metrics_population_is_known;

ALTER TABLE outcome.prediction_metrics
    ADD CONSTRAINT prediction_metrics_population_is_known
    CHECK (population IN ('all', 'maturity_cohort'));

CREATE INDEX IF NOT EXISTS prediction_metrics_population_idx
    ON outcome.prediction_metrics (computed_at DESC, population, window_label);
