-- Bellwether — 007 M2: point-in-time editor and page state
--
-- Idempotent.
--
-- These tables hold the state as of the LAST event processed. They are not a
-- history: they are the accumulator a replay leaves behind, so that online
-- scoring in M3 can ask "what did we know about this editor" without replaying
-- thirty days of events for every incoming edit.
--
-- Correctness comes from the order of operations, not from the schema. For
-- each event, in ascending event_ts: read the state, emit features, THEN fold
-- the event in. Emitting after folding would include the event in its own
-- history — the most ordinary way to build a model that cannot be reproduced
-- in production.

-- ---------------------------------------------------------------------------
-- Who performed each revert
--
-- outcome.revert_events already records reverting edits for the whole feed,
-- outside the sampling frame. Adding the actor makes "this editor has
-- performed N reverts" computable for every editor, not only the 3 per cent of
-- registered ones the frame keeps.
--
-- That matters because M2-FR-13 anticipates history features being useless
-- under a 3 per cent sample: most registered editors will appear with no prior
-- edits at all. Reverting activity is the one editor signal this project can
-- observe completely, and it is a strong one — patrollers are prolific
-- reverters and are almost never reverted themselves.
-- ---------------------------------------------------------------------------
ALTER TABLE outcome.revert_events
    ADD COLUMN IF NOT EXISTS revert_user_id bigint;

CREATE INDEX IF NOT EXISTS revert_events_user_idx
    ON outcome.revert_events (revert_user_id, revert_ts);


-- ---------------------------------------------------------------------------
-- landing.editor_state
--
-- Keyed on user_name rather than user_id: logged-out editors on this wiki are
-- temporary accounts with real user ids, but an IP edit on another wiki has
-- none, and the key must work for both.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS landing.editor_state (
    user_key          text        PRIMARY KEY,
    first_seen_utc    timestamptz NOT NULL,
    last_seen_utc     timestamptz NOT NULL,
    edits_seen        integer     NOT NULL DEFAULT 0,
    reverts_performed integer     NOT NULL DEFAULT 0,
    edits_reverted    integer     NOT NULL DEFAULT 0,

    CONSTRAINT editor_state_seen_ordered CHECK (last_seen_utc >= first_seen_utc)
);


-- ---------------------------------------------------------------------------
-- landing.page_state
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS landing.page_state (
    page_key        text        PRIMARY KEY,
    first_seen_utc  timestamptz NOT NULL,
    last_seen_utc   timestamptz NOT NULL,
    edits_seen      integer     NOT NULL DEFAULT 0,
    edits_reverted  integer     NOT NULL DEFAULT 0,

    CONSTRAINT page_state_seen_ordered CHECK (last_seen_utc >= first_seen_utc)
);


-- ---------------------------------------------------------------------------
-- Grants
--
-- These are DERIVED and regenerable by replay, so unlike the evidence tables
-- the writer may update them in place. Rebuilding is the recovery path, which
-- is exactly why they are not evidence and are not sealed.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON landing.editor_state TO bellwether_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON landing.page_state   TO bellwether_writer;
GRANT SELECT ON landing.editor_state TO bellwether_readonly;
GRANT SELECT ON landing.page_state   TO bellwether_readonly;
