-- 016 — separating "cannot be checked" from "did not match" (M3-FR-18).
--
-- The first production run of the reproducibility job reported 5 of 496
-- unreproducible, and the number was not trustworthy in either direction: the
-- job replayed only the two days it was checking, so a prediction whose editor
-- or page was first seen before that window could never have matched. That is
-- not a prediction that fails to reproduce. It is one the run had no business
-- claiming to have checked.
--
-- Published as its own count rather than folded into either side. A job that
-- quietly drops what it cannot verify reports a clean agreement rate over a
-- shrinking denominator, which looks better every time it gets worse.
ALTER TABLE register.reproductions
    ADD COLUMN IF NOT EXISTS state_predates_window integer NOT NULL DEFAULT 0;

-- The constraint from 015 counted only the three outcomes that existed then.
ALTER TABLE register.reproductions
    DROP CONSTRAINT IF EXISTS reproductions_counts_are_consistent;

ALTER TABLE register.reproductions
    ADD CONSTRAINT reproductions_counts_are_consistent
    CHECK (hash_matched + matched_at_scoring_time + unreproducible
           + state_predates_window <= sampled);
