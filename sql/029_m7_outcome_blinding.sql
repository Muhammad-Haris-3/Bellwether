-- 029 — keep the answer out of sight until the reviewer has given theirs (M7 §5).
--
-- The queue displayed an Outcome column. For a settled edit it said "reverted"
-- or "stood", so a reviewer judging one of those was agreeing with the answer
-- in front of them — and kappa would come out near 1.0 for a reason that means
-- nothing at all.
--
-- This is the human version of M3-FR-10. The scorer already refuses to count a
-- prediction written after its own outcome became visible, for exactly the same
-- reason, and the same guard was missing for people.
--
-- `app.human_labels.was_matured` has recorded this since M6 and nothing read it.
-- Two changes: the queue now withholds the outcome until a verdict is in, and
-- the study excludes the labels collected before it did.

-- The study needs to see the flag it has been storing.
--
-- Dropped first: CREATE OR REPLACE cannot change a function's return type, and
-- this adds a column to it. Postgres refuses with "cannot change return type of
-- existing function" rather than silently keeping the old shape, which is the
-- right way round — but the migration has to say so.
--
-- Safe because nothing holds a reference to it: the study calls it by name at
-- run time, and the GRANT below is reissued.
DROP FUNCTION IF EXISTS outcome.labels_for_agreement(integer);

CREATE FUNCTION outcome.labels_for_agreement(p_maturity_seconds integer)
RETURNS TABLE (
    revid        bigint,
    queue_slice  text,
    verdict      text,
    reviewer     uuid,
    proxy_reverted boolean,
    outcome_was_visible boolean
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, app, outcome, landing
AS $$
    WITH observed AS (
        SELECT c.revid,
               max(c.age_seconds)          AS last_observed_age,
               bool_or(c.had_reverted_tag) AS ever_positive
          FROM outcome.label_checks c
         GROUP BY c.revid
    )
    SELECT h.revid,
           h.queue_slice,
           h.verdict,
           h.user_id,
           (o.ever_positive
            OR EXISTS (SELECT 1 FROM outcome.labels l
                        WHERE l.revid = h.revid AND l.label)) AS proxy_reverted,
           -- Was the outcome already settled when this person judged? If so the
           -- queue was showing it to them, and their verdict is not independent
           -- evidence about the edit.
           h.was_matured
      FROM app.human_labels h
      JOIN landing.rc_events e ON e.revid = h.revid
      JOIN observed o          ON o.revid = h.revid
     WHERE EXTRACT(epoch FROM now() - e.event_ts) >= p_maturity_seconds
       AND (o.ever_positive OR o.last_observed_age >= p_maturity_seconds)
$$;

REVOKE ALL    ON FUNCTION outcome.labels_for_agreement(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION outcome.labels_for_agreement(integer) TO bellwether_writer;

-- Counted and published, like every other exclusion in this project. The
-- direction of its bias is not subtle: a judgement made with the answer visible
-- agrees with the answer, so including these would inflate kappa toward 1.
ALTER TABLE outcome.label_agreement
    ADD COLUMN IF NOT EXISTS excluded_outcome_visible integer NOT NULL DEFAULT 0;
