-- Bellwether — 012 M3: global point-in-time state
--
-- Idempotent.
--
-- One row per scalar the feature builder needs and no editor or page owns.
-- Currently just the highest user_id seen so far, which is the frontier the
-- drift-stable account feature is measured against.
--
-- WHY THIS EXISTS
--
-- M2's most important feature was log_user_id, and removing it dropped the
-- KC-2 margin from +0.1032 to +0.0441 — below the threshold. It is also
-- guaranteed to drift: account ids only increase, so a model trained on
-- August's magnitudes meets systematically larger ones in September. The
-- project found the mechanism by which its own first model would decay before
-- that model was ever deployed.
--
-- The replacement measures an id against the frontier at the time of the edit
-- rather than in absolute terms. A brand-new account scores near 1 in any
-- month; a veteran scores near 0 in any month. The quantity stops moving even
-- though its inputs never stop.
--
-- The frontier must itself be point-in-time: folded in event by event, in
-- order, exactly like editor and page state. Reading max(user_id) over the
-- whole table would let an edit see accounts created after it — the leak the
-- knowability guard exists to prevent, reintroduced through an aggregate.

CREATE TABLE IF NOT EXISTS landing.pipeline_state (
    state_key      text        PRIMARY KEY,
    value_bigint   bigint,
    updated_at_utc timestamptz NOT NULL DEFAULT now()
);

-- Derived and regenerable by replay, like editor_state and page_state, so the
-- writer may update it in place. Rebuilding is the recovery path — which is
-- exactly why this is not evidence and is not sealed.
GRANT SELECT, INSERT, UPDATE, DELETE ON landing.pipeline_state TO bellwether_writer;
GRANT SELECT ON landing.pipeline_state TO bellwether_readonly;
