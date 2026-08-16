-- Bellwether — 030 M8: what the watchdog has already said
--
-- Idempotent.
--
-- WHY THIS EXISTS
--
-- The watchdog failed its run whenever any fault was present. That is correct
-- for a fault that comes and goes and wrong for one that stays, and the
-- difference was not academic: the reproduction rate fell below 100% on the
-- 14th and the watchdog then failed one hundred consecutive runs. A red mark
-- that is always red is not a signal, and while it was on, a seven-hour ingest
-- outage came and went underneath it without anyone noticing — which is the
-- precise failure the watchdog was built to prevent.
--
-- So the alert becomes an edge rather than a level. A fault that was not here
-- last time fails the run; a fault that has been here for days is printed with
-- its age and re-raised once a day, so it can neither be missed nor drown
-- everything else.
--
-- Bookkeeping, NOT evidence. This table records what the watchdog has said,
-- not what it observed — the observations live in landing.run_log and
-- register.reproductions, which are append-only and sealed. Nothing here is a
-- claim about the world, so unlike the register the writer may update and
-- delete rows, and losing the whole table costs one noisy run while it
-- rediscovers what is currently broken.
CREATE TABLE IF NOT EXISTS landing.watchdog_faults (
    -- The fault's identity, NOT its message. "ingest last ran 77 minutes ago"
    -- and "ingest last ran 83 minutes ago" are one continuing fault, and
    -- keying on the text would make every run a fresh alert about it —
    -- rebuilding the always-red behaviour this table exists to end.
    fault_key    text        PRIMARY KEY,
    first_seen   timestamptz NOT NULL DEFAULT now(),
    last_seen    timestamptz NOT NULL DEFAULT now(),

    -- When this fault last turned a run red. Distinct from last_seen, which is
    -- every run it was present for.
    last_alerted timestamptz NOT NULL DEFAULT now(),

    -- The most recent wording, kept so the printed report can carry the
    -- current numbers rather than the ones from when the fault opened.
    message      text        NOT NULL
);

CREATE INDEX IF NOT EXISTS watchdog_faults_first_seen_idx
    ON landing.watchdog_faults (first_seen);

GRANT SELECT, INSERT, UPDATE, DELETE ON landing.watchdog_faults TO bellwether_writer;
GRANT SELECT                         ON landing.watchdog_faults TO bellwether_readonly;
