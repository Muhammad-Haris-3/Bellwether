-- 021 — the segment boundaries a decision is judged over (M5-FR-6).
--
-- PREREGISTRATION.md §4 fixes two of the four decision segments as QUARTILES:
-- edit size band, and page activity band. Quartiles of what, computed when, is
-- left to the implementation, and getting it wrong is not subtle.
--
-- Recomputed per evaluation window, the boundaries would move under the metric.
-- A segment could then regress because the bands shifted rather than because
-- the model did, and P-5 would block a promotion — or wave one through — for a
-- reason that has nothing to do with either model.
--
-- So they are computed once from the TRAINING window and frozen with the model
-- version, here, beside the feature list and the hyperparameters that are
-- already frozen the same way.
ALTER TABLE register.model_registry
    ADD COLUMN IF NOT EXISTS segment_bands jsonb;

-- The page activity band counts edits to the same page in the 7 days before an
-- event, per §4. That is a correlated lookup per row, run once per training
-- window and again for every promotion decision, and rc_events is indexed on
-- revid and event_ts but not on title.
CREATE INDEX IF NOT EXISTS rc_events_title_ts_idx
    ON landing.rc_events (title, event_ts);

COMMENT ON COLUMN register.model_registry.segment_bands IS
    'Quartile boundaries for the pre-registered decision segments, frozen at '
    'training time. Null for models trained before M5, whose decisions record '
    'which model the bands were taken from instead.';
