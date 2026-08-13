-- Bellwether — 004 M1: gap healing (M1-FR-12 to FR-14)
--
-- Idempotent, like every file here.

-- ---------------------------------------------------------------------------
-- landing.gap_attempts
--
-- Insert-only log of every attempt to heal a hole in coverage.
--
-- Gaps themselves are NOT stored. They are derived from rc_events on demand,
-- because a stored gap goes stale the moment part of it fills: its recorded
-- boundaries would describe a hole that no longer has those edges, and the
-- healer would keep re-requesting a window it had already closed.
--
-- What is worth storing is the attempt — when we tried, over what window, and
-- how many rows it produced. A window attempted repeatedly and yielding
-- nothing is not a bug to retry forever; it is a permanent gap, and saying so
-- out loud is the difference between a coverage figure that is honest and one
-- that is merely optimistic.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS landing.gap_attempts (
    attempt_id      bigserial   PRIMARY KEY,
    gap_from_utc    timestamptz NOT NULL,
    gap_to_utc      timestamptz NOT NULL,
    attempted_at    timestamptz NOT NULL DEFAULT now(),
    rows_added      integer     NOT NULL DEFAULT 0,
    api_calls       integer     NOT NULL DEFAULT 0,
    run_id          uuid,

    CONSTRAINT gap_attempts_ordered CHECK (gap_to_utc > gap_from_utc)
);

CREATE INDEX IF NOT EXISTS gap_attempts_window_idx
    ON landing.gap_attempts (gap_from_utc, gap_to_utc);

GRANT SELECT, INSERT ON landing.gap_attempts TO bellwether_writer;
GRANT SELECT          ON landing.gap_attempts TO bellwether_readonly;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA landing TO bellwether_writer;
