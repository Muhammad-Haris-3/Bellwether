-- Bellwether — 032 M9: fold each event into state exactly once
--
-- Idempotent.
--
-- WHY THIS EXISTS
--
-- reconcile measured 23,616 divergences across 62,929 in-window keys — 62.47%
-- agreement between replayed and persisted state — and the largest categories
-- were the plainest ones: page.edits (8,993) and editor.edits (4,578). The
-- sample lines said what was happening without ambiguity:
--
--     editor 'Up2show'.edits: replay says 5, stored says 10
--
-- Exactly double. State is folded as a SIDE EFFECT of scoring, and the
-- scorer's event set is model-dependent: UNSCORED_SQL gates on
-- `p.model_version = %(model_version)s`, so when a champion changes, every
-- event inside the lookback window that the old champion scored becomes
-- eligible again for the new one. It is re-scored, and state.observe folds it
-- a SECOND time. The registry reports two model versions; one promotion, one
-- extra fold, counts exactly doubled.
--
-- The prediction insert is idempotent through ON CONFLICT, which is why this
-- went unnoticed for four days: re-scoring writes no duplicate evidence. The
-- state fold had no equivalent guard, so it accumulated in silence.
--
-- The pattern is not new here. landing.state_applied_reverts has done exactly
-- this for reverts since M3 — a revert is folded once, ever, and the query
-- that finds pending ones excludes what it already holds. Reverts got
-- idempotent folding; ordinary events never did. This is that table's sibling.
--
-- WHAT THIS DOES NOT FIX
--
-- Two known gaps remain, recorded rather than papered over:
--
--   * A re-scored event still reads a history that already contains its own
--     first fold, because the state it loads was persisted with that event in
--     it. Skipping the second fold stops the inflation; it does not make the
--     re-scored prediction's history point-in-time correct. Only folding
--     independently of scoring does that.
--   * The scorer refuses to score inside the champion's training window, so it
--     never folds those events at all, while a replay folds every raw event in
--     its window. That is the other direction of drift — stored BELOW replay —
--     and it is inherent to folding-during-scoring.
--
-- Both belong to decoupling state from scoring, which is a larger change than
-- this one and is not attempted here.

CREATE TABLE IF NOT EXISTS landing.state_applied_events (
    -- The edit whose contribution is already in the persisted counters. Primary
    -- key, so a concurrent second writer conflicts rather than double-counts.
    revid          bigint      PRIMARY KEY,
    applied_at_utc timestamptz NOT NULL DEFAULT now(),

    -- Which path folded it: 'score' for the ordinary online path, 'replay' for
    -- a full rebuild. Kept because the two have different blind spots — the
    -- online path skips the training window, the replay does not — and a future
    -- divergence is much easier to read when the ledger says who wrote what.
    applied_by     text        NOT NULL DEFAULT 'score',

    CONSTRAINT state_applied_events_source CHECK (applied_by IN ('score', 'replay'))
);

COMMENT ON TABLE landing.state_applied_events IS
    'Events already folded into landing.editor_state and landing.page_state. Prevents a re-scored event being counted twice; sibling of state_applied_reverts.';

-- The read is "which of these revids do we already hold", always against a
-- bounded batch, so the primary key serves it. The timestamp index exists for
-- retention, which will eventually need to trim this alongside rc_events.
CREATE INDEX IF NOT EXISTS state_applied_events_applied_idx
    ON landing.state_applied_events (applied_at_utc);

GRANT SELECT, INSERT, DELETE ON landing.state_applied_events TO bellwether_writer;
GRANT SELECT ON landing.state_applied_events TO bellwether_readonly;

-- DELETE is granted, unlike on the register: this is bookkeeping about a
-- derived table, not evidence. Retention will need to trim it, and a full
-- rebuild is entitled to reset it.
