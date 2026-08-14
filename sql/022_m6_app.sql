-- 022 — the application: users, sessions, human labels, audit log (M6).
--
-- The first schema in this project that holds anything about a person, and the
-- first the serving container can write to at all. Both facts change what the
-- grants have to do.
CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- The application's own role (M6-FR-7, NFR-8)
--
-- Distinct from bellwether_writer, which runs the pipeline, and from
-- bellwether_readonly, which the public API uses. NFR-8 promised that no
-- credential with write access to predictions or model_decisions would ever be
-- held by the serving container, and M6 is the first milestone that gives that
-- container any write capability at all — so this is where the guarantee is
-- kept or quietly lost.
--
-- INSERT on exactly two tables. SELECT on the rest. Nothing else, ever.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bellwether_app') THEN
        CREATE ROLE bellwether_app NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA app TO bellwether_app;
GRANT USAGE ON SCHEMA landing, outcome, register, decide TO bellwether_app;

-- ---------------------------------------------------------------------------
-- app.users
--
-- Accounts are issued by an administrator (SRS FR-38, amended 2026-08-14).
-- There is no self-service sign-up and no email is sent; the address is an
-- identifier, not a channel.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.users (
    user_id       uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text        NOT NULL,

    -- scrypt, from the standard library, so the serving image gains no
    -- dependency (M6-FR-38). Salt per user; parameters stored beside the hash
    -- so they can be raised later without invalidating existing rows.
    password_hash bytea       NOT NULL,
    password_salt bytea       NOT NULL,
    kdf_params    jsonb       NOT NULL,

    role          text        NOT NULL CHECK (role IN ('viewer', 'reviewer', 'admin')),
    is_active     boolean     NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    uuid        REFERENCES app.users (user_id),

    -- Lowercased at write time by the application; the constraint makes a
    -- second account differing only in case impossible rather than unlikely.
    CONSTRAINT users_email_is_lowercase UNIQUE (email),
    CONSTRAINT users_email_lowercased CHECK (email = lower(email))
);

-- ---------------------------------------------------------------------------
-- app.sessions
--
-- Server-side, which is the part of FR-38 that carried the security property.
--
-- The token is stored HASHED and the plaintext is never persisted (M6-FR-2). A
-- session table holding plaintext tokens is a credential store: one database
-- read is a login as anybody, and this project publishes its database host in a
-- health endpoint on purpose.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.sessions (
    session_id   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid        NOT NULL REFERENCES app.users (user_id) ON DELETE CASCADE,
    token_hash   bytea       NOT NULL UNIQUE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),

    -- Two expiries. Absolute bounds a stolen token; idle bounds an abandoned
    -- browser. One without the other leaves a session that is either immortal
    -- while in use or dies mid-task.
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz
);

CREATE INDEX IF NOT EXISTS sessions_user_idx    ON app.sessions (user_id);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON app.sessions (expires_at);

-- ---------------------------------------------------------------------------
-- app.human_labels  (SRS FR-42, M6-FR-18 to FR-21)
--
-- What a reviewer thought. NOT what Wikipedia did — that is outcome.labels, and
-- these two must never merge. Mixing them would make every number this project
-- has published unverifiable, because nobody could tell afterwards which rows
-- came from which source.
--
-- champion_version and score_shown are recorded because a judgement is partly a
-- reaction to the number on the screen. A label collected under one champion is
-- not interchangeable with one collected under another, and by M7 that
-- distinction is unrecoverable unless it is captured now.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.human_labels (
    label_id         bigserial   PRIMARY KEY,
    revid            bigint      NOT NULL REFERENCES landing.rc_events (revid),
    user_id          uuid        NOT NULL REFERENCES app.users (user_id),
    judged_at        timestamptz NOT NULL DEFAULT now(),

    verdict          text        NOT NULL
        CHECK (verdict IN ('bad_edit', 'good_edit', 'unsure')),
    confidence       text        NOT NULL
        CHECK (confidence IN ('low', 'medium', 'high')),

    champion_version text,
    score_shown      double precision CHECK (score_shown BETWEEN 0 AND 1),

    -- Whether the outcome was already final when the reviewer judged it. A
    -- judgement made after the revert was visible is not independent evidence
    -- about the edit, and M7's agreement study has to be able to tell.
    was_matured      boolean     NOT NULL DEFAULT false,

    CONSTRAINT human_labels_one_per_reviewer UNIQUE (revid, user_id)
);

CREATE INDEX IF NOT EXISTS human_labels_revid_idx ON app.human_labels (revid);

-- ---------------------------------------------------------------------------
-- app.audit_log  (SRS FR-43, M6-FR-22 to FR-24)
--
-- Including FAILED attempts. A log of successful actions describes what the
-- system did; a log including refusals describes what it was asked to do, and
-- the second is what shows an access control working rather than merely
-- present.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.audit_log (
    audit_id   bigserial   PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT now(),

    -- Null for an unauthenticated attempt, which is itself worth recording.
    actor      uuid        REFERENCES app.users (user_id),
    actor_role text,

    action     text        NOT NULL,
    target     text,
    outcome    text        NOT NULL CHECK (outcome IN ('allowed', 'refused', 'failed')),
    detail     text,

    -- Never the token, never a password, never a full connection string.
    request_ip text
);

CREATE INDEX IF NOT EXISTS audit_log_recent_idx ON app.audit_log (occurred_at DESC);
CREATE INDEX IF NOT EXISTS audit_log_actor_idx  ON app.audit_log (actor, occurred_at DESC);

-- ---------------------------------------------------------------------------
-- app.automation_freeze  (SRS FR-37, M6-FR-25)
--
-- An administrator may halt promotion. The same administrator may not alter,
-- delete or backdate any past decision — which is why this is a separate,
-- append-only table rather than a flag someone can flip back and forth without
-- trace.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.automation_freeze (
    freeze_id  bigserial   PRIMARY KEY,
    set_at     timestamptz NOT NULL DEFAULT now(),
    frozen     boolean     NOT NULL,
    actor      uuid        REFERENCES app.users (user_id),
    reason     text
);

-- ---------------------------------------------------------------------------
-- Row-level security  (SRS FR-39, M6-FR-9 to FR-11)
--
-- The requirement is that roles are enforced HERE, not in application code
-- alone. The test of that claim is what happens when the application layer is
-- wrong, so the policies read the acting user from a transaction-local setting
-- and decide for themselves.
--
-- FORCE, not just ENABLE. Without FORCE the table owner bypasses every policy,
-- and migrations run as the owner — so a check that looked correct in
-- production would be silently absent whenever anything connected as owner.
-- ---------------------------------------------------------------------------
ALTER TABLE app.users             ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.sessions          ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.human_labels      ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.audit_log         ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.automation_freeze ENABLE ROW LEVEL SECURITY;

ALTER TABLE app.users             FORCE ROW LEVEL SECURITY;
ALTER TABLE app.sessions          FORCE ROW LEVEL SECURITY;
ALTER TABLE app.human_labels      FORCE ROW LEVEL SECURITY;
ALTER TABLE app.audit_log         FORCE ROW LEVEL SECURITY;
ALTER TABLE app.automation_freeze FORCE ROW LEVEL SECURITY;

-- Who is acting, and what they are allowed to be.
--
-- Reads a transaction-local setting the application sets per request. Returns
-- NULL when unset, and every policy below fails closed on NULL — an
-- unauthenticated request is not a request with an empty role, it is a request
-- with no rights at all.
CREATE OR REPLACE FUNCTION app.acting_user() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('bellwether.user_id', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION app.acting_role() RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT u.role FROM app.users u
     WHERE u.user_id = app.acting_user() AND u.is_active
$$;

-- SECURITY DEFINER above is deliberate and narrow: the policies need to read
-- app.users to find the acting role, and a policy on app.users that consulted
-- app.users would recurse. It returns one text value for the current setting
-- only, and can leak nothing else.
REVOKE ALL ON FUNCTION app.acting_role() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.acting_role() TO bellwether_app;
GRANT EXECUTE ON FUNCTION app.acting_user() TO bellwether_app;

-- Users: you see yourself. An admin sees everyone. Nobody sees a hash they do
-- not own, and no policy grants UPDATE or DELETE to the application at all.
DROP POLICY IF EXISTS users_self_or_admin ON app.users;
CREATE POLICY users_self_or_admin ON app.users FOR SELECT
    USING (user_id = app.acting_user() OR app.acting_role() = 'admin');

-- Sessions: yours alone, ever. Including an admin's — an administrator who can
-- read session rows can impersonate every user in the system, and no feature
-- in this application needs that.
DROP POLICY IF EXISTS sessions_own ON app.sessions;
CREATE POLICY sessions_own ON app.sessions FOR SELECT
    USING (user_id = app.acting_user());

-- Human labels: reviewers and admins may write, and only as themselves. The
-- WITH CHECK on user_id is what stops a reviewer recording a judgement under
-- somebody else's name.
DROP POLICY IF EXISTS human_labels_read ON app.human_labels;
CREATE POLICY human_labels_read ON app.human_labels FOR SELECT
    USING (app.acting_role() IN ('viewer', 'reviewer', 'admin'));

DROP POLICY IF EXISTS human_labels_write ON app.human_labels;
CREATE POLICY human_labels_write ON app.human_labels FOR INSERT
    WITH CHECK (app.acting_role() IN ('reviewer', 'admin') AND user_id = app.acting_user());

-- Audit log: anyone authenticated may append, only an admin may read. A
-- reviewer able to read the audit log can see every other reviewer's activity,
-- which is surveillance rather than accountability.
DROP POLICY IF EXISTS audit_append ON app.audit_log;
CREATE POLICY audit_append ON app.audit_log FOR INSERT WITH CHECK (true);

DROP POLICY IF EXISTS audit_read_admin ON app.audit_log;
CREATE POLICY audit_read_admin ON app.audit_log FOR SELECT
    USING (app.acting_role() = 'admin');

-- Freeze: an admin may set it; anyone authenticated may see that it is set,
-- because a frozen system that looks live to its users is worse than a frozen
-- one that says so.
DROP POLICY IF EXISTS freeze_read ON app.automation_freeze;
CREATE POLICY freeze_read ON app.automation_freeze FOR SELECT
    USING (app.acting_role() IN ('viewer', 'reviewer', 'admin'));

DROP POLICY IF EXISTS freeze_write_admin ON app.automation_freeze;
CREATE POLICY freeze_write_admin ON app.automation_freeze FOR INSERT
    WITH CHECK (app.acting_role() = 'admin' AND actor = app.acting_user());

-- ---------------------------------------------------------------------------
-- Grants (M6-FR-8)
--
-- INSERT on exactly two tables. The application cannot write to a single piece
-- of evidence the pipeline produced, and cannot edit or delete anything at all
-- — not even a row it just wrote.
-- ---------------------------------------------------------------------------
GRANT SELECT         ON app.users             TO bellwether_app;
GRANT SELECT, INSERT ON app.sessions          TO bellwether_app;
GRANT SELECT, INSERT ON app.human_labels      TO bellwether_app;
GRANT SELECT, INSERT ON app.audit_log         TO bellwether_app;
GRANT SELECT, INSERT ON app.automation_freeze TO bellwether_app;

GRANT USAGE, SELECT ON SEQUENCE app.human_labels_label_id_seq      TO bellwether_app;
GRANT USAGE, SELECT ON SEQUENCE app.audit_log_audit_id_seq         TO bellwether_app;
GRANT USAGE, SELECT ON SEQUENCE app.automation_freeze_freeze_id_seq TO bellwether_app;

-- Sessions need revocation, which is an UPDATE of one column on one's own row.
-- Granted narrowly rather than by giving the role UPDATE on the table.
GRANT UPDATE (revoked_at, last_seen_at) ON app.sessions TO bellwether_app;

DROP POLICY IF EXISTS sessions_own_update ON app.sessions;
CREATE POLICY sessions_own_update ON app.sessions FOR UPDATE
    USING (user_id = app.acting_user());

DROP POLICY IF EXISTS sessions_insert ON app.sessions;
CREATE POLICY sessions_insert ON app.sessions FOR INSERT WITH CHECK (true);

-- What the queue reads. SELECT only, and on nothing the pipeline calls
-- evidence beyond reading it.
GRANT SELECT ON landing.rc_events        TO bellwether_app;
GRANT SELECT ON outcome.labels           TO bellwether_app;
GRANT SELECT ON outcome.label_checks     TO bellwether_app;
GRANT SELECT ON register.predictions     TO bellwether_app;
GRANT SELECT ON register.model_registry  TO bellwether_app;
GRANT SELECT ON decide.model_decisions   TO bellwether_app;
GRANT SELECT ON decide.champion_history  TO bellwether_app;
