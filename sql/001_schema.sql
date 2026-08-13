-- Bellwether — 001 Schema (M0 subset)
--
-- Two schemas only. The SRS describes five layers (landing, derived, register,
-- outcome, decide); M0 needs the first and the fourth, and creating the others
-- before anything lives in them would be scaffolding pretending to be design.
--
--   landing  — what we observed, exactly as observed
--   outcome  — what later turned out to be true
--
-- Idempotent. Safe to re-run.

CREATE SCHEMA IF NOT EXISTS landing;
CREATE SCHEMA IF NOT EXISTS outcome;


-- ---------------------------------------------------------------------------
-- landing.run_log  (FR-5)
--
-- Every job execution, including the ones that fail. A log of successes only
-- would be silent at exactly the moment it is needed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS landing.run_log (
    run_log_id       bigserial PRIMARY KEY,
    run_id           uuid        NOT NULL,
    job              text        NOT NULL,
    window_from_utc  timestamptz,
    window_to_utc    timestamptz,
    started_at_utc   timestamptz NOT NULL,
    finished_at_utc  timestamptz,
    api_calls        integer     NOT NULL DEFAULT 0,
    rows_read        integer     NOT NULL DEFAULT 0,
    rows_written     integer     NOT NULL DEFAULT 0,
    status           text        NOT NULL
        CHECK (status IN ('running', 'success', 'partial', 'failed')),
    error_class      text,
    error_detail     text
);

CREATE INDEX IF NOT EXISTS run_log_job_started_idx
    ON landing.run_log (job, started_at_utc DESC);


-- ---------------------------------------------------------------------------
-- landing.cursors  (FR-2)
--
-- One row per job. The cursor advances only after the rows it covers are
-- committed, so a crashed run re-reads its window rather than skipping it.
--
-- Re-reading is safe because rc_events is keyed on revid and inserts use
-- ON CONFLICT DO NOTHING. This matters more than it looks: recentchanges
-- timestamps have one-second resolution and many edits share a second, so a
-- cursor stored as a timestamp CANNOT be exclusive without risking a skipped
-- edit. It is therefore deliberately inclusive — every run re-requests the
-- final second of the previous run and discards the duplicates. Overlap is
-- cheap; a hole is permanent.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS landing.cursors (
    job             text PRIMARY KEY,
    position_utc    timestamptz NOT NULL,
    updated_at_utc  timestamptz NOT NULL DEFAULT now(),
    updated_by_run  uuid
);


-- ---------------------------------------------------------------------------
-- landing.rc_events
--
-- One row per observed edit. Insert-only.
--
-- sampling_stratum and sampling_weight are recorded from the first row onward
-- even though M0 ingests everything at weight 1.0. The frame in SRS 6.3 is a
-- case-control design, and a population estimate computed from rows whose
-- weight was reconstructed after the fact is not the same thing as one
-- computed from weights recorded at observation time. Writing them now makes
-- M1 a configuration change rather than a backfill.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS landing.rc_events (
    revid             bigint      PRIMARY KEY,
    old_revid         bigint,
    rcid              bigint,
    event_ts          timestamptz NOT NULL,
    ns                integer     NOT NULL,
    title             text        NOT NULL,
    user_name         text,
    user_id           bigint,

    -- Two distinct notions of "not logged in", kept apart on purpose.
    --
    -- is_anon is the classic IP edit. is_temp is a temporary account, the
    -- replacement English Wikipedia now uses: a logged-out editor is given an
    -- auto-created account (e.g. "~2026-44334-20") instead of having their IP
    -- published. Measured on 2026-08-13, is_anon was true for 0 of 2,498
    -- main-namespace edits and is_temp for about 13% — so a frame built on
    -- is_anon alone would sample nothing at all.
    is_anon           boolean     NOT NULL,
    is_temp           boolean     NOT NULL DEFAULT false,
    is_minor          boolean     NOT NULL,
    is_bot            boolean     NOT NULL,
    is_patrolled      boolean,
    comment           text,
    comment_hidden    boolean     NOT NULL DEFAULT false,
    user_hidden       boolean     NOT NULL DEFAULT false,
    oldlen            integer,
    newlen            integer,
    tags              text[]      NOT NULL DEFAULT '{}',
    sampling_stratum  text        NOT NULL,
    sampling_weight   numeric     NOT NULL DEFAULT 1.0
        CHECK (sampling_weight > 0),
    ingested_at_utc   timestamptz NOT NULL DEFAULT now(),
    ingest_run_id     uuid,

    -- We cannot have observed an edit before it happened.
    --
    -- The five-minute tolerance is for clock skew, not for slack: MediaWiki
    -- stamps event_ts on its own clock and ingested_at_utc comes from ours.
    -- A strict inequality would turn a few seconds of NTP drift on a GitHub
    -- runner into a hard ingestion failure, which trades a real outage for a
    -- theoretical guarantee. The guarantee that actually matters — that a
    -- score was not issued before the edit existed — belongs on the
    -- predictions register in M3, where the clocks are both ours.
    CONSTRAINT rc_events_not_ingested_before_event
        CHECK (ingested_at_utc >= event_ts - interval '5 minutes')
);

CREATE INDEX IF NOT EXISTS rc_events_event_ts_idx
    ON landing.rc_events (event_ts);

-- Supports the "which events are due a label check" query, which filters on
-- age and is the hottest query in the labelling job.
CREATE INDEX IF NOT EXISTS rc_events_event_ts_revid_idx
    ON landing.rc_events (event_ts, revid);


-- ---------------------------------------------------------------------------
-- outcome.label_checks  (M0-T4, and the raw material for M2)
--
-- One row per (event, checkpoint) observation. This table exists because the
-- interesting question is not "was it reverted" but "how long did it take to
-- find out" — and that can only be answered by recording the checks that found
-- nothing, at the age they found nothing.
--
-- Deleting the negative observations would leave only the positives, from
-- which no survival curve can be estimated. They are the data.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outcome.label_checks (
    check_id           bigserial   PRIMARY KEY,
    revid              bigint      NOT NULL REFERENCES landing.rc_events (revid),
    checkpoint_seconds bigint      NOT NULL,
    checked_at_utc     timestamptz NOT NULL,
    age_seconds        bigint      NOT NULL CHECK (age_seconds >= 0),
    had_reverted_tag   boolean     NOT NULL,
    rev_missing        boolean     NOT NULL DEFAULT false,
    run_id             uuid,

    CONSTRAINT label_checks_one_per_checkpoint UNIQUE (revid, checkpoint_seconds)
);

CREATE INDEX IF NOT EXISTS label_checks_revid_idx
    ON outcome.label_checks (revid);


-- ---------------------------------------------------------------------------
-- outcome.labels  (FR-9, FR-10)
--
-- The first observation of an outcome, per source. UNIQUE (revid, label_source)
-- with ON CONFLICT DO NOTHING is how FR-10 is enforced: a later re-observation
-- cannot overwrite the moment we first knew.
--
-- Two sources are recorded independently and never reconciled in place:
--   mw_reverted — the mw-reverted tag on the reverted edit  (primary)
--   revert_tag  — mw-undo / mw-rollback / mw-manual-revert on the reverting
--                 edit, mapped back                          (secondary)
-- Their disagreement rate is a published data-quality figure (FR-11), which
-- is only possible if both are kept.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outcome.labels (
    label_id                   bigserial   PRIMARY KEY,
    revid                      bigint      NOT NULL REFERENCES landing.rc_events (revid),
    label                      boolean     NOT NULL,
    label_source               text        NOT NULL
        CHECK (label_source IN ('mw_reverted', 'revert_tag')),
    first_observed_at_utc      timestamptz NOT NULL,

    -- When the revert actually happened, where the source can tell us.
    -- The primary path usually cannot: the tag says "was reverted", not when.
    revert_latency_seconds     bigint CHECK (revert_latency_seconds >= 0),

    -- When WE found out. Always known, and the quantity that bounds how fast
    -- the system can possibly learn.
    detection_latency_seconds  bigint      NOT NULL CHECK (detection_latency_seconds >= 0),

    revert_revid               bigint,
    observed_run_id            uuid,

    CONSTRAINT labels_one_per_source UNIQUE (revid, label_source)
);

CREATE INDEX IF NOT EXISTS labels_revid_idx  ON outcome.labels (revid);
CREATE INDEX IF NOT EXISTS labels_source_idx ON outcome.labels (label_source, label);
