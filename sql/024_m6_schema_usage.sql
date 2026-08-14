-- 024 — let the other roles see that the app schema exists (M6).
--
-- sql/022 created schema `app` and granted USAGE to bellwether_app alone. The
-- health endpoint runs as bellwether_readonly and probes every migration with
-- to_regclass, and to_regclass requires USAGE on the schema — without it the
-- probe raises InsufficientPrivilege rather than returning NULL.
--
-- So /health went fully dark: database_reachable false, an empty schema map,
-- and status degraded, on a database where everything was applied and working.
-- One new schema took down the endpoint that reports on all twenty-three.
--
-- USAGE on a schema grants nothing readable by itself — every table in `app`
-- still has no SELECT for these roles, and RLS still applies to the one role
-- that does. It permits resolving a name, which is all the health probe needs.
GRANT USAGE ON SCHEMA app TO bellwether_readonly, bellwether_writer;

-- M6-FR-36. Expired sessions are deleted by the maintenance job, which runs as
-- the writer. Sessions are the one table in this schema that is NOT evidence:
-- they are credentials with an expiry, and leaving them to accumulate is both a
-- storage leak and a growing set of rows that should not exist.
--
-- DELETE only, and only here. The writer cannot read a session, insert one, or
-- alter one — it can remove what has already expired.
GRANT DELETE ON app.sessions TO bellwether_writer;

DROP POLICY IF EXISTS sessions_prune_expired ON app.sessions;
CREATE POLICY sessions_prune_expired ON app.sessions FOR DELETE
    USING (expires_at < now());

-- The policy is the guard, not the job's WHERE clause. A pruning query with a
-- mistaken predicate would otherwise delete live sessions and sign everybody
-- out, and the only thing standing between that and production would be the
-- care taken writing one line of SQL.
