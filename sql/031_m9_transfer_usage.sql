-- Bellwether — 031 M9: what the reads actually cost
--
-- Idempotent.
--
-- WHY THIS EXISTS
--
-- NFR-4 caps STORAGE at 80 percent of the Neon Free allowance, and
-- bellwether.retention reports against it every day. Nothing capped DATA
-- TRANSFER, which is a second allowance on the same plan, metered in bytes read
-- out of the database, with a worse failure mode.
--
-- Storage running out refuses writes. Transfer running out refuses
-- CONNECTIONS. The retention module already notes that a storage failure
-- "surfaces as ingestion failing for reasons that look nothing like a storage
-- problem"; a transfer failure surfaces as ingestion, scoring, the watchdog and
-- the API all failing at once, on a message about a quota, from a database that
-- was working an hour ago.
--
-- That is not a projection. GridCast — same author, same plan, same
-- architecture — spent its transfer allowance on 2026-08-17 and stopped dead.
-- The API returned 500s, the pages went blank, and because the pipeline reads
-- too, its append-only register stopped growing. The register could not even be
-- exported, because exporting is reading. Nothing had been counting, so the
-- first signal was a refused connection.
--
-- Bellwether runs roughly 227 scheduled jobs a day, against the same ceiling.
-- One row here per job invocation. The value is the TREND: a query that starts
-- returning ten times more than it did is invisible until something adds it up,
-- and that is the failure this table exists to make loud — rather than the
-- ceiling, which arrives too late to act on.
--
-- Bookkeeping, NOT evidence. Nothing about a prediction's standing depends on
-- it, and unlike the register it is safe to delete. It is deliberately NOT
-- folded into landing.run_log, whose rows_read column counts records returned
-- by the MediaWiki API; sharing that column would break a documented meaning
-- and make two unrelated quantities indistinguishable.
CREATE TABLE IF NOT EXISTS landing.db_transfer (
    transfer_id     bigserial   PRIMARY KEY,

    -- Null for the jobs that run outside a RunContext, which is most of the
    -- read-only ones. Recorded where available so a spike can be attributed to
    -- the run that caused it.
    run_id          uuid,
    job             text        NOT NULL,
    recorded_at_utc timestamptz NOT NULL DEFAULT now(),

    queries         integer     NOT NULL,
    rows_returned   bigint      NOT NULL,

    -- ESTIMATED, and named so nobody has to read the module to discover it.
    -- Measured from the width of the values returned, not observed on the wire:
    -- it sees neither protocol framing nor TLS nor compression, and it will
    -- disagree with the provider's figure. Good enough to expose a regression
    -- on the day it lands, which is the whole of its job.
    bytes_estimated bigint      NOT NULL,

    code_commit     text        NOT NULL,

    CONSTRAINT db_transfer_counts_sane
        CHECK (queries >= 0 AND rows_returned >= 0 AND bytes_estimated >= 0)
);

COMMENT ON TABLE landing.db_transfer IS
    'Estimated database egress per job invocation. Bookkeeping, not evidence — safe to prune.';
COMMENT ON COLUMN landing.db_transfer.bytes_estimated IS
    'Estimated from returned value widths, not measured on the wire. Will disagree with the provider figure. Exists to expose a trend, not to report remaining allowance.';

-- The only access pattern: everything since the billing period began.
CREATE INDEX IF NOT EXISTS db_transfer_recorded_idx
    ON landing.db_transfer (recorded_at_utc DESC);

-- Deletable, unlike the register. Retention is a future decision and the grant
-- should not be the thing that prevents it.
GRANT SELECT, INSERT, DELETE ON landing.db_transfer TO bellwether_writer;
GRANT SELECT ON landing.db_transfer TO bellwether_readonly;
GRANT USAGE, SELECT ON SEQUENCE landing.db_transfer_transfer_id_seq TO bellwether_writer;
