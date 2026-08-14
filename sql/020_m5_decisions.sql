-- 020 — self-maintenance: triggers, shadow, and the decision log (M5).
--
-- The milestone the project exists for, and the one where it starts acting
-- without a human. The only thing that makes that defensible is that the rule
-- was written down before any model existed and every decision it makes is
-- recorded with the evidence that produced it.
CREATE SCHEMA IF NOT EXISTS decide;
GRANT USAGE ON SCHEMA decide TO bellwether_writer, bellwether_readonly;

-- ---------------------------------------------------------------------------
-- decide.trigger_evaluations  (M5-FR-11 to FR-14)
--
-- Every daily evaluation, including the ones that fire nothing. A table holding
-- only the firings cannot answer "was this checked yesterday", and a trigger
-- that silently stopped being evaluated looks exactly like a trigger that keeps
-- not firing.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decide.trigger_evaluations (
    evaluation_id    bigserial   PRIMARY KEY,
    evaluated_at     timestamptz NOT NULL DEFAULT now(),

    -- The day this evaluation stands for, not the moment it ran. A run delayed
    -- past midnight still describes its own window, and consecutiveness is
    -- counted in these rather than in run times.
    window_day       date        NOT NULL,

    champion_version text        NOT NULL,

    -- Measured values, stored whether or not they crossed anything. The
    -- threshold lives in bellwether/preregistration.py; storing the measurement
    -- means a later reader can apply the rule themselves rather than trust that
    -- it was applied.
    rolling_pr_auc   double precision,
    baseline_pr_auc  double precision,
    pr_auc_drop      double precision,
    max_psi          double precision,
    max_psi_feature  text,
    days_since_train integer,

    decay_breached   boolean     NOT NULL DEFAULT false,
    drift_breached   boolean     NOT NULL DEFAULT false,
    floor_breached   boolean     NOT NULL DEFAULT false,

    -- How many consecutive days each condition has now held. Stored rather than
    -- recomputed, because M5-FR-12 requires a GAP to reset the count and a
    -- query over "the last three rows" cannot tell a gap from a delay.
    decay_streak     integer     NOT NULL DEFAULT 0,
    drift_streak     integer     NOT NULL DEFAULT 0,

    fired            boolean     NOT NULL DEFAULT false,
    fired_reason     text,

    n_matured        integer     NOT NULL DEFAULT 0,
    code_commit      text,
    run_id           uuid,

    CONSTRAINT trigger_evaluations_one_per_day UNIQUE (window_day, champion_version)
);

CREATE INDEX IF NOT EXISTS trigger_evaluations_recent_idx
    ON decide.trigger_evaluations (window_day DESC);

-- ---------------------------------------------------------------------------
-- decide.psi_features  (M5-FR-13)
--
-- Per-feature drift, so a firing names what moved. `max_psi` alone would say
-- the inputs changed without saying which, which is a trigger nobody can act
-- on and an alert nobody can dismiss.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decide.psi_features (
    psi_id        bigserial PRIMARY KEY,
    evaluation_id bigint    NOT NULL REFERENCES decide.trigger_evaluations (evaluation_id),
    feature       text      NOT NULL,
    psi           double precision NOT NULL,

    CONSTRAINT psi_features_one_per_evaluation UNIQUE (evaluation_id, feature)
);

-- ---------------------------------------------------------------------------
-- decide.model_decisions  (M5-FR-29 to FR-33)
--
-- Promotions, REJECTIONS and rollbacks, with the same evidence attached to each.
--
-- A log containing only promotions answers "what changed" and cannot answer
-- "what was considered and refused" — which is the more interesting question,
-- and the one that shows the rule actually binding rather than being satisfied
-- by everything that reached it.
--
-- Append-only by grant, like every other piece of evidence in this schema. A
-- decision that could be edited afterwards is a decision nobody has to stand by.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decide.model_decisions (
    decision_id       bigserial   PRIMARY KEY,
    decided_at        timestamptz NOT NULL DEFAULT now(),

    decision          text        NOT NULL
        CHECK (decision IN ('promote', 'reject', 'rollback')),

    champion_version   text       NOT NULL,
    challenger_version text,
    trigger_reason     text,

    -- Every P-condition, measured and judged, on the row (M5-FR-30, FR-31).
    -- A decision must be reconstructible from this row alone, without database
    -- access to anything else — the same standard the metric card, the artifact
    -- digest in git and the monthly seal are held to. A decision that needs the
    -- database to interpret is one only its owner can check.
    p1_pr_auc_gain      double precision,
    p1_pass             boolean,
    p2_ci_low           double precision,
    p2_ci_high          double precision,
    p2_pass             boolean,
    p3_matured_positives integer,
    p3_pass             boolean,
    p4_shadow_days      double precision,
    p4_pass             boolean,
    p5_ece_regression   double precision,
    p5_worst_segment    text,
    p5_worst_segment_regression double precision,
    p5_pass             boolean,

    champion_pr_auc    double precision,
    challenger_pr_auc  double precision,
    n_matured          integer,
    n_positives        integer,

    -- Which pre-registration the rule was read from. The constants live in
    -- code and are asserted against the document, so recording the commit makes
    -- the version of the rule that was applied recoverable years later.
    prereg_commit      text,
    code_commit        text,
    run_id             uuid
);

CREATE INDEX IF NOT EXISTS model_decisions_recent_idx
    ON decide.model_decisions (decided_at DESC);

-- ---------------------------------------------------------------------------
-- The champion pointer (M5-FR-24)
--
-- registry.champion() has meant "most recently registered" since M3, which was
-- named as a placeholder in sql/013 when it was written. It now means "the
-- model this decision log promoted", and this is where that is recorded.
--
-- Deliberately NOT a column on model_registry: the registry records what was
-- trained, and which of those is serving is a decision, not a property of the
-- artifact. Keeping them apart is what lets a rollback restore a champion
-- without rewriting anything about the model itself.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decide.champion_history (
    history_id     bigserial   PRIMARY KEY,
    model_version  text        NOT NULL,
    effective_from timestamptz NOT NULL DEFAULT now(),
    decision_id    bigint      REFERENCES decide.model_decisions (decision_id),

    -- The version this replaced, so a rollback target is on the row rather than
    -- inferred by ordering.
    replaced       text
);

CREATE INDEX IF NOT EXISTS champion_history_recent_idx
    ON decide.champion_history (effective_from DESC);

-- Evidence.
REVOKE UPDATE, DELETE, TRUNCATE ON decide.model_decisions      FROM bellwether_writer;
REVOKE UPDATE, DELETE, TRUNCATE ON decide.champion_history     FROM bellwether_writer;
REVOKE UPDATE, DELETE, TRUNCATE ON decide.trigger_evaluations  FROM bellwether_writer;
REVOKE UPDATE, DELETE, TRUNCATE ON decide.psi_features         FROM bellwether_writer;

GRANT SELECT, INSERT ON decide.model_decisions     TO bellwether_writer;
GRANT SELECT, INSERT ON decide.champion_history    TO bellwether_writer;
GRANT SELECT, INSERT ON decide.trigger_evaluations TO bellwether_writer;
GRANT SELECT, INSERT ON decide.psi_features        TO bellwether_writer;

GRANT SELECT ON ALL TABLES IN SCHEMA decide TO bellwether_readonly;

GRANT USAGE, SELECT ON SEQUENCE decide.model_decisions_decision_id_seq       TO bellwether_writer;
GRANT USAGE, SELECT ON SEQUENCE decide.champion_history_history_id_seq       TO bellwether_writer;
GRANT USAGE, SELECT ON SEQUENCE decide.trigger_evaluations_evaluation_id_seq TO bellwether_writer;
GRANT USAGE, SELECT ON SEQUENCE decide.psi_features_psi_id_seq               TO bellwether_writer;
