-- 014 — the reverts already folded into online state (M3-FR-12).
--
-- The online path learns of a revert long after the edit was scored, and has to
-- fold it into the editor and page counters exactly once. Not zero times, which
-- is the bug this table fixes; and not twice, which would be worse because
-- nothing would ever say so.
--
-- Keyed on the REVERTED revid rather than the reverting one. An edit can be
-- named by more than one revert_event and by a label as well, and all of them
-- describe the same fact: this edit was reverted. It counts once.
--
-- A watermark on a timestamp was the alternative and is not safe here. Rows
-- inserted in one transaction all carry the same now(), and a transaction that
-- starts early but commits late writes a row behind a watermark that has
-- already moved past it — silently never applied. An explicit set has no such
-- window.
CREATE TABLE IF NOT EXISTS landing.state_applied_reverts (
    revid          bigint      PRIMARY KEY,
    applied_at_utc timestamptz NOT NULL DEFAULT now(),

    -- Whether the counters actually moved. An edit whose editor row does not
    -- exist online cannot be incremented, and recording that separately keeps
    -- "we handled this" distinct from "we changed something".
    counters_moved boolean     NOT NULL DEFAULT true
);

CREATE INDEX IF NOT EXISTS state_applied_reverts_applied_idx
    ON landing.state_applied_reverts (applied_at_utc);

-- Derived and regenerable, like editor_state and page_state: the recovery path
-- is to delete it and replay. That is exactly why it is not evidence and is not
-- sealed.
GRANT SELECT, INSERT, UPDATE, DELETE ON landing.state_applied_reverts TO bellwether_writer;
GRANT SELECT ON landing.state_applied_reverts TO bellwether_readonly;
