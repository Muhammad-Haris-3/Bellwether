-- Bellwether — 006 M1: revert events observed outside the sampling frame
--
-- Idempotent.
--
-- WHY THIS EXISTS
--
-- The sampling frame governs what the project STUDIES. It should never have
-- governed what the project can OBSERVE about outcomes, and until this file it
-- silently did.
--
-- Measured on census data: 93.8 per cent of reverting edits are made by
-- registered editors, and the M1 frame samples registered editors at 3 per
-- cent. The secondary label path — which derives outcomes from the reverting
-- edit's own tags, at zero API cost — would therefore have seen about 6 per
-- cent of the reverts it used to see. Its recall, 19 per cent under a census,
-- would have fallen to roughly 1 per cent.
--
-- Nothing would have failed. No error, no alert: a path that used to
-- contribute labels would simply have stopped contributing them, and the
-- agreement figure between the two paths would have quietly become
-- meaningless. That is the failure mode this project keeps finding, and this
-- time the frame itself would have caused it.
--
-- So reverting edits are recorded for the WHOLE feed, regardless of the frame.
-- The row is deliberately narrow — an identifier, its target, a timestamp and
-- a method. At a measured 3.86 per cent of ~90,000 edits a day, ninety days
-- costs roughly 25 MB of a 400 MB budget.

CREATE TABLE IF NOT EXISTS outcome.revert_events (
    revert_revid    bigint      PRIMARY KEY,
    reverted_revid  bigint      NOT NULL,
    revert_ts       timestamptz NOT NULL,
    method          text        NOT NULL
        CHECK (method IN ('mw-undo', 'mw-rollback', 'mw-manual-revert')),
    observed_at_utc timestamptz NOT NULL DEFAULT now(),
    observed_by_run uuid,

    -- A revert cannot precede what it reverts. This is the same guard the
    -- secondary path applies before writing a label, moved to where it cannot
    -- be forgotten: a mis-parsed edit summary naming an unrelated revision
    -- would otherwise produce a negative latency and a silently wrong label.
    CONSTRAINT revert_events_targets_something_else
        CHECK (reverted_revid <> revert_revid)
);

CREATE INDEX IF NOT EXISTS revert_events_reverted_idx
    ON outcome.revert_events (reverted_revid);

CREATE INDEX IF NOT EXISTS revert_events_ts_idx
    ON outcome.revert_events (revert_ts);

-- Evidence, like everything else in this schema.
REVOKE UPDATE, DELETE, TRUNCATE ON outcome.revert_events FROM bellwether_writer;
GRANT  SELECT, INSERT            ON outcome.revert_events TO   bellwether_writer;
GRANT  SELECT                    ON outcome.revert_events TO   bellwether_readonly;
