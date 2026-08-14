-- 027 — where a human label came from, and what it can be used for (M7 §2).
--
-- A queue ranked by the model, labelled by a human, fed back into the model is
-- a machine for confirming what the model already believes. The reviewer sees
-- the top of the ranking, those labels enter training, and the next model
-- receives almost no signal about the items it scored LOW — which is where its
-- false negatives live, and they are the errors that matter.
--
-- The fix is a slice of the queue drawn at random rather than by rank. This
-- column records which slice each label came from, and it is the difference
-- between an agreement figure that answers BQ-8 and one that measures agreement
-- among edits the model already flagged.
ALTER TABLE app.human_labels
    ADD COLUMN IF NOT EXISTS queue_slice text NOT NULL DEFAULT 'ranked'
        CHECK (queue_slice IN ('ranked', 'random'));

COMMENT ON COLUMN app.human_labels.queue_slice IS
    'Which slice of the queue this item was drawn from. Set at judgement time '
    'from the server''s own selection, never from the client, and never '
    'back-filled — a slice inferred afterwards is not a slice.';

-- Whether the reviewer could see the model''s score when they judged.
--
-- M6 displayed it. That anchors the judgement: a reviewer shown 0.92 is not
-- forming an independent opinion about the edit, they are agreeing or
-- disagreeing with a number, and BQ-8 asks what a human thinks the edit was.
--
-- M7 withholds the score until after the verdict is recorded. The handful of
-- M6-era labels were collected with it visible, so they are marked as such
-- rather than quietly folded in with the rest.
ALTER TABLE app.human_labels
    ADD COLUMN IF NOT EXISTS score_was_visible boolean NOT NULL DEFAULT true;

ALTER TABLE app.human_labels
    ALTER COLUMN score_was_visible SET DEFAULT false;

CREATE INDEX IF NOT EXISTS human_labels_slice_idx
    ON app.human_labels (queue_slice, judged_at DESC);

-- ---------------------------------------------------------------------------
-- outcome.label_agreement  (M7-FR-11 to FR-15)
--
-- How good a proxy "was reverted" is for "was a bad edit". Every figure this
-- project has published rests on that substitution and nothing has checked it.
--
-- Append-only like every other measurement. A run that refuses to produce a
-- figure is still recorded, with the reason: "we have not measured this yet"
-- and "we measured it and there was not enough data" are different states, and
-- an empty table cannot tell them apart.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outcome.label_agreement (
    agreement_id  bigserial   PRIMARY KEY,
    computed_at   timestamptz NOT NULL DEFAULT now(),

    -- Which labels this was computed over. 'random' is the only slice that can
    -- answer BQ-8; 'ranked' is published only as agreement conditioned on the
    -- model's own ranking, and is labelled as such wherever it appears.
    queue_slice   text        NOT NULL CHECK (queue_slice IN ('ranked', 'random', 'all')),

    -- How `unsure` was handled. It is never silently dropped: those are the
    -- ambiguous cases, where the proxy is most likely to disagree with a human,
    -- which is exactly what this study is about. Excluding them selects on the
    -- outcome being studied, so both treatments are computed and labelled.
    unsure_policy text        NOT NULL CHECK (unsure_policy IN ('excluded', 'as_good')),

    n             integer     NOT NULL,
    n_reviewers   integer     NOT NULL,
    n_unsure      integer     NOT NULL,
    unsure_rate   double precision,

    -- The confusion matrix, whatever it shows. Human verdict against the revert
    -- proxy: "bad" and "reverted" are the positive classes.
    both_positive integer,
    human_only    integer,
    proxy_only    integer,
    both_negative integer,

    kappa         double precision,
    observed_agreement  double precision,
    expected_agreement  double precision,

    -- Null kappa is a state with a reason, not a gap. Below the threshold the
    -- figure is refused and this says why.
    refused_reason text,

    maturity_hours integer    NOT NULL,
    code_commit   text,
    run_id        uuid
);

CREATE INDEX IF NOT EXISTS label_agreement_recent_idx
    ON outcome.label_agreement (computed_at DESC);

REVOKE UPDATE, DELETE, TRUNCATE ON outcome.label_agreement FROM bellwether_writer;
GRANT  SELECT, INSERT            ON outcome.label_agreement TO   bellwether_writer;
GRANT  SELECT                    ON outcome.label_agreement TO   bellwether_readonly, bellwether_app;
GRANT USAGE, SELECT ON SEQUENCE outcome.label_agreement_agreement_id_seq TO bellwether_writer;

-- The agreement job runs as the writer and must read human labels, which live
-- behind RLS keyed on an authenticated app user the writer never sets.
--
-- The same narrow-door pattern as sql/023: a function that returns exactly what
-- the study needs — verdict, slice, and the proxy outcome — and cannot be asked
-- who judged what. Reviewer identity leaves as a COUNT and never as a name.
CREATE OR REPLACE FUNCTION outcome.labels_for_agreement(p_maturity_seconds integer)
RETURNS TABLE (
    revid        bigint,
    queue_slice  text,
    verdict      text,
    reviewer     uuid,
    proxy_reverted boolean
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
                        WHERE l.revid = h.revid AND l.label)) AS proxy_reverted
      FROM app.human_labels h
      JOIN landing.rc_events e ON e.revid = h.revid
      JOIN observed o          ON o.revid = h.revid
     -- The M4 maturity rule, unchanged. A human label on an edit whose outcome
     -- is not settled has nothing to be compared against, and including it
     -- would count "nobody has checked" as "not reverted".
     WHERE EXTRACT(epoch FROM now() - e.event_ts) >= p_maturity_seconds
       AND (o.ever_positive OR o.last_observed_age >= p_maturity_seconds)
$$;

REVOKE ALL    ON FUNCTION outcome.labels_for_agreement(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION outcome.labels_for_agreement(integer) TO bellwether_writer;
