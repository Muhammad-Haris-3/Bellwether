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
