-- Bellwether — 033 M9: retry a revert whose row was not there yet
--
-- Idempotent.
--
-- WHY THIS EXISTS
--
-- Every apply_reverts run reports the same shape:
--
--     state: folded 40 newly discovered reverts
--            (23 moved a counter, 17 had no row online)
--
-- Seventeen of forty dropped, every run, continuously. Those are the
-- editor_edits_reverted (1,716) and page.reverted (1,873) divergences that
-- reconcile measures and that reproduce --explain named in production: the
-- replay folds the revert because the edit is in rc_events, and the online path
-- could not, because no editor_state or page_state row existed to increment.
--
-- A row is absent when that editor or page was never folded — most often
-- because the edit sits outside the scorer's lookback, or inside the champion's
-- training window, which the scorer refuses to score and therefore never folds.
--
-- TWO DEFECTS, NOT ONE
--
-- 1. A dropped revert was recorded as handled. That was deliberate — "or every
--    run would retry it forever and the count would never mean anything" — and
--    it is right about the risk. But the row often appears LATER, when the
--    editor edits again and is scored, and by then the revert is marked done
--    and will never be applied. The counter stays permanently short.
--
-- 2. Worse, and not deliberate: apply_reverts summed the editor and page
--    rowcounts into one `touched`. A revert that incremented the editor but
--    found no page row counted as moved and was marked fully handled, so the
--    PAGE counter stayed short forever while the record claimed success. The
--    existing counters_moved column cannot distinguish the two sides, so it
--    could not have shown this.
--
-- THE FIX, AND WHY IT CANNOT SIMPLY RETRY EVERYTHING
--
-- Each side is now tracked separately and retried until it lands, bounded by
-- the same window apply_reverts already walks — so a revert whose row never
-- appears ages out of the query instead of being retried forever, which is the
-- concern the original comment was protecting against.
--
-- Rows written BEFORE this migration carry NULL in both new columns, and NULL
-- means "do not retry". This is the one part that must not be clever: those
-- rows record only whether SOMETHING moved, so a retry could re-apply a revert
-- that was already counted and inflate the very counters this exists to
-- correct. Under-counting a known amount is recoverable; double-counting
-- silently is not. The historical shortfall is repaired by reconcile, under a
-- recorded decision, not by guessing here.

ALTER TABLE landing.state_applied_reverts
    ADD COLUMN IF NOT EXISTS editor_moved boolean,
    ADD COLUMN IF NOT EXISTS page_moved   boolean;

COMMENT ON COLUMN landing.state_applied_reverts.editor_moved IS
    'Whether the editor counter was incremented. NULL for rows written before sql/033, meaning unknown and never to be retried — a retry could double-count.';
COMMENT ON COLUMN landing.state_applied_reverts.page_moved IS
    'Whether the page counter was incremented. NULL means unknown; see editor_moved.';

-- The pending query looks for rows with a side still outstanding, so it reads
-- this rather than the primary key alone.
CREATE INDEX IF NOT EXISTS state_applied_reverts_outstanding_idx
    ON landing.state_applied_reverts (revid)
 WHERE editor_moved IS NOT NULL AND NOT (editor_moved AND page_moved);

-- Retrying needs to update the row it already wrote.
GRANT UPDATE (editor_moved, page_moved, counters_moved) ON landing.state_applied_reverts
    TO bellwether_writer;
