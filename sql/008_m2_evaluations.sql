-- Bellwether — 008 M2: the evaluation record
--
-- Idempotent.
--
-- C-8 requires KC-2 to be answered IN PUBLIC. A kill criterion decided in a
-- job log that needs a login to read is not a kill criterion; it is a private
-- opinion with a number attached.
--
-- So every evaluation run appends a row here, and the API serves the latest.
-- Append-only, like the rest of the evidence: a verdict that could be replaced
-- once it became inconvenient would be worth nothing, and this project's whole
-- claim is that its own bad results survive contact with publication.

CREATE TABLE IF NOT EXISTS outcome.evaluations (
    evaluation_id   bigserial   PRIMARY KEY,
    evaluated_at    timestamptz NOT NULL DEFAULT now(),

    -- What was evaluated, and under what assumptions. Stored beside the result
    -- because a PR-AUC without its maturity window and window bounds is not a
    -- number anyone can check.
    window_start    timestamptz NOT NULL,
    window_end      timestamptz NOT NULL,
    maturity_hours  integer     NOT NULL,
    provisional     boolean     NOT NULL DEFAULT true,

    n_events        integer     NOT NULL,
    n_positives     integer     NOT NULL,
    n_features      integer     NOT NULL,

    -- PR-AUC per scorer, including every baseline. Kept as jsonb so a baseline
    -- added later does not require a migration and, more importantly, so no
    -- baseline can be dropped from a report without the row showing it.
    pr_auc          jsonb       NOT NULL,

    margin          double precision NOT NULL,
    ci_low          double precision NOT NULL,
    ci_high         double precision NOT NULL,
    margin_required double precision NOT NULL,
    clears_kc2      boolean     NOT NULL,

    feature_names   text[]      NOT NULL,
    code_commit     text,
    run_id          uuid
);

CREATE INDEX IF NOT EXISTS evaluations_at_idx ON outcome.evaluations (evaluated_at DESC);

REVOKE UPDATE, DELETE, TRUNCATE ON outcome.evaluations FROM bellwether_writer;
GRANT  SELECT, INSERT            ON outcome.evaluations TO   bellwether_writer;
GRANT  SELECT                    ON outcome.evaluations TO   bellwether_readonly;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA outcome TO bellwether_writer;
