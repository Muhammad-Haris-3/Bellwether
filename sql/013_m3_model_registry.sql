-- Bellwether — 013 M3: the model registry
--
-- Idempotent.
--
-- One append-only row per trained model. What was trained, from what window,
-- on which features, with which hyperparameters, and the SHA-256 of the
-- artifact it produced.
--
-- Append-only because this is the other half of the evidence. A score in
-- register.predictions is only interpretable against the model that made it,
-- and a registry that could be edited would let the model behind a good result
-- be described differently after the fact.
--
-- WHICH MODEL IS THE CHAMPION
--
-- In M3 there is one model and the champion is simply the most recently
-- registered. That is a placeholder and is named as one. M5 replaces it with
-- the promotion rule fixed in PREREGISTRATION.md, decided by evidence rather
-- than by recency, and records each decision in its own append-only table.
--
-- Building that mechanism now would mean inventing a promotion procedure
-- before there is a second model to promote, which is how a rule ends up
-- shaped by the convenience of the moment rather than by the pre-registration.

CREATE TABLE IF NOT EXISTS register.model_registry (
    model_version    text        PRIMARY KEY,
    trained_at       timestamptz NOT NULL DEFAULT now(),

    training_start   timestamptz NOT NULL,
    training_end     timestamptz NOT NULL,
    n_train_events   integer     NOT NULL,
    n_train_positives integer    NOT NULL,

    feature_names    text[]      NOT NULL,
    hyperparameters  jsonb       NOT NULL,
    offline_metrics  jsonb       NOT NULL,

    -- The artifact lives in git, not here. A promotion should be verifiable by
    -- someone with the repository and no database access at all, and a blob in
    -- a table nobody can reach is not evidence to them.
    artifact_path    text        NOT NULL,
    artifact_sha256  text        NOT NULL,

    code_commit      text,
    registered_by_run uuid,

    CONSTRAINT model_registry_window_ordered CHECK (training_end > training_start),
    CONSTRAINT model_registry_sha_length     CHECK (length(artifact_sha256) = 64)
);

CREATE INDEX IF NOT EXISTS model_registry_trained_at_idx
    ON register.model_registry (trained_at DESC);

REVOKE UPDATE, DELETE, TRUNCATE ON register.model_registry FROM bellwether_writer;
GRANT  SELECT, INSERT            ON register.model_registry TO   bellwether_writer;
GRANT  SELECT                    ON register.model_registry TO   bellwether_readonly;
