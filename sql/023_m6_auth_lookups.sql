-- 023 — the two lookups that must happen before anyone is authenticated (M6 §3).
--
-- The policies on app.users and app.sessions both gate on app.acting_user(),
-- which is set from a verified session. Sign-in has to read app.users to check
-- a password, and establishing a session has to read app.sessions to find out
-- who is asking — both BEFORE there is an acting user to gate on.
--
-- So the policy that protects the table forbids the query that would satisfy
-- it. Every authenticated system meets this; the fix is a narrow, audited door
-- rather than a looser policy, because loosening the policy would open the
-- whole table to every authenticated request as well.
--
-- These two functions are that door. SECURITY DEFINER, so they bypass RLS;
-- executable only by bellwether_app; and each returns exactly the columns one
-- specific step needs for one specific key. Neither takes a predicate, so
-- neither can be turned into "list the users".

-- Sign-in: everything needed to verify a password for ONE address, and nothing
-- else. The hash and salt leave the table here and nowhere else.
CREATE OR REPLACE FUNCTION app.credentials_for(p_email text)
RETURNS TABLE (
    user_id       uuid,
    role          text,
    password_hash bytea,
    password_salt bytea,
    kdf_params    jsonb,
    is_active     boolean
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = app, pg_temp
AS $$
    SELECT u.user_id, u.role, u.password_hash, u.password_salt, u.kdf_params, u.is_active
      FROM app.users u
     WHERE u.email = lower(p_email)
$$;

-- Establishing a session: the row for ONE token hash. Keyed on the hash rather
-- than on a user, so holding this grant lets the caller resolve a token it
-- already has and nothing more — it cannot enumerate sessions or find another
-- user's.
CREATE OR REPLACE FUNCTION app.session_for(p_token_hash bytea)
RETURNS TABLE (
    session_id   uuid,
    user_id      uuid,
    expires_at   timestamptz,
    last_seen_at timestamptz,
    revoked_at   timestamptz,
    email        text,
    role         text,
    is_active    boolean
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = app, pg_temp
AS $$
    SELECT s.session_id, s.user_id, s.expires_at, s.last_seen_at, s.revoked_at,
           u.email, u.role, u.is_active
      FROM app.sessions s
      JOIN app.users u ON u.user_id = s.user_id
     WHERE s.token_hash = p_token_hash
$$;

-- SET search_path on both is not decoration. A SECURITY DEFINER function
-- without it resolves unqualified names against the caller's search_path, and a
-- caller who can create objects could shadow a table it references and have it
-- run as the definer.

REVOKE ALL ON FUNCTION app.credentials_for(text)  FROM PUBLIC;
REVOKE ALL ON FUNCTION app.session_for(bytea)     FROM PUBLIC;

GRANT EXECUTE ON FUNCTION app.credentials_for(text) TO bellwether_app;
GRANT EXECUTE ON FUNCTION app.session_for(bytea)    TO bellwether_app;
