-- 015 — reproducibility runs (M3-FR-17, M3-FR-18).
--
-- FR-17 says a stored prediction shall be recomputable. FR-18 is the one that
-- makes it mean something: a job re-derives a sample and publishes the
-- agreement rate, because a reproducibility claim nobody re-checks is a
-- comment.
--
-- Evidence, so append-only like the rest. A run that found 71% agreement is
-- exactly the row someone would want to lose.
CREATE TABLE IF NOT EXISTS register.reproductions (
    reproduction_id  bigserial   PRIMARY KEY,
    ran_at           timestamptz NOT NULL DEFAULT now(),
    window_start     timestamptz NOT NULL,
    window_end       timestamptz NOT NULL,

    sampled          integer     NOT NULL CHECK (sampled >= 0),
    hash_matched     integer     NOT NULL CHECK (hash_matched >= 0),
    score_matched    integer     NOT NULL CHECK (score_matched >= 0),

    -- Of the hashes that did NOT match under the training-time definition of
    -- state, how many matched once the reverts discovered between the edit and
    -- its scoring were folded in. This is the difference between "we cannot
    -- reproduce our own predictions" and "we can, and here is exactly which
    -- definition of state the scorer was using".
    matched_at_scoring_time integer NOT NULL DEFAULT 0,

    unreproducible   integer     NOT NULL DEFAULT 0,
    model_versions   text[]      NOT NULL DEFAULT '{}',
    code_commit      text,
    run_id           uuid,

    CONSTRAINT reproductions_counts_are_consistent
        CHECK (hash_matched + matched_at_scoring_time + unreproducible <= sampled)
);

CREATE INDEX IF NOT EXISTS reproductions_ran_idx ON register.reproductions (ran_at DESC);

REVOKE UPDATE, DELETE, TRUNCATE ON register.reproductions FROM bellwether_writer;
GRANT  SELECT, INSERT            ON register.reproductions TO   bellwether_writer;
GRANT  SELECT                    ON register.reproductions TO   bellwether_readonly;
GRANT  USAGE, SELECT ON SEQUENCE register.reproductions_reproduction_id_seq TO bellwether_writer;
