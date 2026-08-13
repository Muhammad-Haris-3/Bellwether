-- Bellwether — 002 Roles and grants
--
-- This file is where "append-only" stops being a promise and becomes a
-- property. Append-only enforced by code convention is a claim about the
-- author's discipline. Enforced by a grant, it holds even if the author is
-- careless, or dishonest, or replaced.
--
-- The tables that matter are not the big ones. They are the small ones that
-- record what we believed and when we believed it — outcome.labels now,
-- register.predictions and decide.model_decisions from M3 and M5. A system
-- that can rewrite those can claim any accuracy it likes.
--
--   bellwether_writer    — GitHub Actions jobs.
--                          INSERT everywhere. UPDATE/DELETE only on the two
--                          tables that are working state rather than evidence.
--   bellwether_readonly  — the serving API. SELECT, and nothing else, ever.
--
-- Idempotent. CREATE ROLE has no IF NOT EXISTS, hence the DO blocks.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bellwether_writer') THEN
        CREATE ROLE bellwether_writer;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bellwether_readonly') THEN
        CREATE ROLE bellwether_readonly;
    END IF;
END
$$;


-- ---------------------------------------------------------------------------
-- Let the owner assume both roles.
--
-- Needed so the append-only guarantee can be VERIFIED — SET ROLE is the only
-- way to test a grant without holding that role's password, and a guarantee
-- that can only be checked when someone happens to have credentials to hand is
-- one that stops being checked.
--
-- This weakens nothing. The owner already owns every table in both schemas and
-- so bypasses their grants entirely; membership adds no capability it did not
-- already have. What it adds is the ability to demonstrate, on any run, that
-- the roles the pipeline and the API actually use cannot rewrite history.
--
-- PostgreSQL 16 changed role membership so a creating role no longer
-- automatically gets usable SET on the created role in every configuration.
-- Stating it explicitly means the verification does not depend on a default
-- that varies between a local server and a managed provider.
-- ---------------------------------------------------------------------------
-- Best-effort, deliberately. Granting a role requires ADMIN OPTION on it, and
-- roles are cluster-wide while databases are not — so an owner can inherit
-- roles some other owner created and hold no ADMIN on them. Letting that abort
-- the migration would make an inconvenience for the verifier into a failure of
-- the schema, which is the wrong order of importance.
--
-- When it does not apply, the bootstrap script says so and falls back to
-- reading has_table_privilege() from the catalogue, which needs no membership.
DO $$
BEGIN
    BEGIN
        EXECUTE format('GRANT bellwether_writer TO %I', current_user);
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE NOTICE 'no ADMIN on bellwether_writer; SET ROLE verification unavailable';
    END;
    BEGIN
        EXECUTE format('GRANT bellwether_readonly TO %I', current_user);
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE NOTICE 'no ADMIN on bellwether_readonly; SET ROLE verification unavailable';
    END;
END
$$;


-- ---------------------------------------------------------------------------
-- bellwether_writer
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA landing, outcome TO bellwether_writer;

GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA landing TO bellwether_writer;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA outcome TO bellwether_writer;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA landing TO bellwether_writer;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA outcome TO bellwether_writer;

-- Working state, not evidence. The cursor must move; the run log's row is
-- opened when a job starts and closed when it ends, so it must be updatable.
GRANT UPDATE, DELETE ON landing.cursors TO bellwether_writer;
GRANT UPDATE          ON landing.run_log TO bellwether_writer;

-- Evidence. Explicit REVOKE rather than merely withholding the grant: the
-- intent should be visible in the file, and it survives someone later adding a
-- careless GRANT ALL above it.
REVOKE UPDATE, DELETE, TRUNCATE ON landing.rc_events    FROM bellwether_writer;
REVOKE UPDATE, DELETE, TRUNCATE ON outcome.labels       FROM bellwether_writer;
REVOKE UPDATE, DELETE, TRUNCATE ON outcome.label_checks FROM bellwether_writer;

-- rc_events is exempt from the no-DELETE rule at M1, when the 120-day
-- retention job (SRS 6.5) needs it. That will be a narrowed grant on that one
-- table with its own justification, not a relaxation of this file.


-- ---------------------------------------------------------------------------
-- bellwether_readonly
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA landing, outcome TO bellwether_readonly;

GRANT SELECT ON ALL TABLES IN SCHEMA landing TO bellwether_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA outcome TO bellwether_readonly;


-- ---------------------------------------------------------------------------
-- Defaults for tables created by later migrations
--
-- Without these, every table added in M1 onward is invisible to the API until
-- someone remembers to grant it — a failure that presents as a bug in the
-- frontend and gets debugged in the wrong place entirely.
-- ---------------------------------------------------------------------------
ALTER DEFAULT PRIVILEGES IN SCHEMA landing
    GRANT SELECT ON TABLES TO bellwether_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA outcome
    GRANT SELECT ON TABLES TO bellwether_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA landing
    GRANT SELECT, INSERT ON TABLES TO bellwether_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA outcome
    GRANT SELECT, INSERT ON TABLES TO bellwether_writer;
