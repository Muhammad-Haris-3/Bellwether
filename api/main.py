"""The read-only API (M0-T8, FR-46).

Serves what the pipeline has accumulated. It holds `bellwether_readonly` and
can do nothing else — the append-only guarantee does not rest on this code
being careful.

Two endpoints in M0:

    GET /health   liveness, and whether the database is reachable
    GET /stats    what is in the database, and how stale it is

Neither returns a secret. `env` is validated to a short label so a connection
string pasted into the wrong variable cannot be published (see
bellwether.config), and the database host is reported with credentials
stripped — deliberately included, because an endpoint answering from the wrong
database looks exactly like an endpoint answering.
"""

from __future__ import annotations

from typing import Any

import psycopg
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from api import sessions
from bellwether.config import get_settings
from bellwether.schema import SCHEMA_EXPECTATIONS

app = FastAPI(
    title="Bellwether API",
    description="Read-only view of a self-maintaining edit-triage service.",
    version="0.1.0",
)

# The frontend is served from a different origin (Vercel), so the browser needs
# permission to read these responses. Everything here is public and read-only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

STATS_SQL = """
SELECT
    (SELECT count(*) FROM landing.rc_events)                                AS events,
    (SELECT count(*) FROM landing.rc_events WHERE sampling_stratum = 'logged_out')
                                                                            AS events_logged_out,
    -- Ingestion-time tags. Useful only as a floor: rc_events is insert-only,
    -- so for a live-ingested edit this array is frozen before any revert can
    -- have happened. Named to say so.
    (SELECT count(*) FROM landing.rc_events WHERE 'mw-reverted' = ANY(tags))
                                                                            AS tagged_at_ingest,
    (SELECT count(*) FROM outcome.labels)                                   AS labels,
    (SELECT count(*) FROM outcome.label_checks)                             AS label_checks,
    -- Reverting edits are recorded for the whole feed, outside the sampling
    -- frame (M1 6.1). Published because the ratio between this and `events` is
    -- the check that the frame has not silently blinded the secondary label
    -- path — the failure it was introduced to prevent.
    (SELECT count(*) FROM outcome.revert_events)                            AS revert_events,
    -- The maturity cohort is a deterministic 10% bucket keyed on revid, and M4
    -- grades it on a 48h window because it is the only slice the labeller
    -- checks that early.
    --
    -- This is FAR below a tenth of events and that is history rather than a
    -- fault: the flag is written at insert time, most rows were inserted before
    -- that code shipped, and ON CONFLICT DO NOTHING means re-ingesting them
    -- never corrects it. Recent ingest runs flag 8.5-12.7 per cent as designed.
    -- first_cohort_event is published beside it so the ratio can be read
    -- against the events the cohort actually covers instead of all of them.
    (SELECT count(*) FROM landing.rc_events WHERE in_maturity_cohort)       AS cohort_events,
    (SELECT min(event_ts) FROM landing.rc_events WHERE in_maturity_cohort)  AS first_cohort_event,
    (SELECT count(*) FROM register.predictions p
       JOIN landing.rc_events e ON e.revid = p.revid
      WHERE e.in_maturity_cohort AND p.role = 'champion')                   AS cohort_predictions,
    (SELECT max(event_ts) FROM landing.rc_events)                           AS newest_event,
    (SELECT min(event_ts) FROM landing.rc_events)                           AS oldest_event
"""

RUNS_SQL = """
SELECT DISTINCT ON (job)
       job,
       status,
       started_at_utc,
       round(EXTRACT(epoch FROM now() - started_at_utc) / 60.0)::int AS minutes_ago
  FROM landing.run_log
 ORDER BY job, started_at_utc DESC
"""

# Coverage, not just endpoints.
#
# Oldest and newest describe a SPAN. A backfill of an old window plus a live
# slice spans three days while covering a fraction of them, and every rate
# computed over that is a rate over disjoint samples. Publishing the gaps makes
# the difference checkable by anyone, rather than by whoever has database
# access — which is also what makes the outage test (M0 A-4) verifiable from
# outside the project.
COVERAGE_SQL = """
WITH ordered AS (
    SELECT event_ts, lag(event_ts) OVER (ORDER BY event_ts) AS prev
      FROM landing.rc_events
),
steps AS (
    SELECT event_ts, prev, event_ts - prev AS delta
      FROM ordered WHERE prev IS NOT NULL
)
SELECT count(*) FILTER (WHERE delta > interval '10 minutes')            AS gap_count,
       COALESCE(EXTRACT(epoch FROM max(delta)), 0)::int                 AS largest_gap_seconds,
       COALESCE(EXTRACT(epoch FROM sum(delta)
                FILTER (WHERE delta > interval '10 minutes')), 0)::int  AS missing_seconds,
       (SELECT prev FROM steps ORDER BY delta DESC LIMIT 1)             AS largest_gap_from,
       (SELECT event_ts FROM steps ORDER BY delta DESC LIMIT 1)         AS largest_gap_to
  FROM steps
"""

# M1-FR-3: every rate available raw AND population-weighted.
#
# The frame is case-control — logged-out edits are kept at 50 per cent and
# registered ones at 3 — so the raw rate over the sample is not the rate in the
# population, and the difference is large. Both are published, always. Choosing
# whichever looked better per table would be exactly the sort of quiet
# selection this project exists to make impossible.
#
# The outcome comes from outcome.labels, NOT from rc_events.tags.
#
# rc_events is insert-only — the writer role cannot UPDATE it — so the tag
# array is frozen at the instant of ingestion. For an edit ingested seconds
# after it was made, that array cannot contain `mw-reverted`, because the
# revert had not happened yet. Scanning it counts reverts only for rows that
# were BACKFILLED late enough to see them.
#
# The published matured rate therefore mixed two populations: backfilled rows,
# where the tag was visible, and live rows, where it structurally never can be.
# It read 22.04 per cent for logged-out editors while the checkpoint data put
# the same figure at 38.21. The append-only guarantee that makes the register
# trustworthy is exactly what made this number wrong.
MATURE_SQL = """
WITH matured AS (
    SELECT e.revid, e.sampling_stratum, e.sampling_weight,
           EXISTS (SELECT 1 FROM outcome.labels l
                    WHERE l.revid = e.revid AND l.label) AS reverted
      FROM landing.rc_events e
     WHERE now() - e.event_ts >= interval '48 hours'
       -- Only events whose outcome has actually been observed. An edit nobody
       -- has checked is not a negative; counting it as one would understate
       -- the rate for the same reason the frozen tag array did.
       AND EXISTS (SELECT 1 FROM outcome.label_checks c WHERE c.revid = e.revid)
)
SELECT sampling_stratum,
       count(*)                                     AS n,
       count(*) FILTER (WHERE reverted)             AS reverted,
       sum(sampling_weight)                         AS weighted_n,
       sum(sampling_weight) FILTER (WHERE reverted) AS weighted_reverted
  FROM matured
 GROUP BY sampling_stratum
 ORDER BY sampling_stratum
"""


# The inputs to the maturity window (M2).
#
# Published because the maturity window decides which predictions are allowed
# to count toward a published accuracy figure. A threshold that important
# should be recomputable by a reader, not taken on trust.
#
# Two corrections are built into the query, and both matter:
#
#   * An event positive at one hour is still positive at six. The labelling job
#     stops checking once an outcome is known, so a naive count of positives at
#     each checkpoint would UNDER-count later ones — the events already known
#     reverted are absent from the later checkpoint entirely. Cumulative
#     incidence is therefore built from the FIRST checkpoint at which each
#     event tested positive, not from per-checkpoint tallies.
#
#   * The denominator is events whose status at that age is KNOWN — observed
#     negative at or beyond it, OR already known positive before it. Counting a
#     two-hour-old edit against the 48-hour checkpoint would treat "has not had
#     time" as "was not reverted", the right-censoring this exercise exists to
#     handle.
#
# The first version of this query got the second point half right. It counted
# only events observed at or beyond each age — and an event that tested
# positive early is never checked again, so it dropped out of the denominator
# at every later age while its revert dropped out of the numerator too. The
# published curve read 10.62%, 1.36%, 0.76%, 21.27% across 1h, 6h, 24h, 48h.
#
# A cumulative incidence cannot fall. That impossibility is the only reason the
# bug was caught, so it is now an assertion in the test suite rather than
# something a reader has to notice.
#   * Ages come from `age_seconds` — when the observation was ACTUALLY made —
#     never from `checkpoint_seconds`, which is only the checkpoint the job was
#     working through. Measured locally, checks nominally due at six hours were
#     performed at eight. Worse, a backfilled event reaches several checkpoints
#     at once and records its 72-hour status against all of them, including the
#     one labelled "1h". That is what produced a curve nearly flat from 6h to
#     24h and then jumping sixteen points at 48h — a shape no revert hazard has.
MATURITY_SQL = """
WITH per_event AS (
    SELECT c.revid,
           max(c.age_seconds)                                   AS last_observed_age,
           min(c.age_seconds) FILTER (WHERE c.had_reverted_tag) AS first_positive_age,
           bool_or(c.in_maturity_cohort)                        AS cohort,
           max(e.sampling_stratum)                              AS stratum
      FROM outcome.label_checks c
      LEFT JOIN landing.rc_events e ON e.revid = c.revid
     GROUP BY c.revid
),
-- A fixed grid, not the checkpoints that happen to exist. The curve should be
-- reported at the same ages whatever the scheduler was doing that week.
grid AS (
    SELECT unnest(ARRAY[3600, 10800, 21600, 43200, 86400, 172800, 259200, 604800]::bigint[])
        AS checkpoint_seconds
)
SELECT g.checkpoint_seconds,
       COALESCE(p.stratum, 'unknown') AS stratum,
       -- KNOWN at this age: either observed negative at or beyond it, or
       -- already known positive before it. The second clause is the one whose
       -- absence produced a non-monotonic curve.
       -- KNOWN at this age: observed at or beyond it, or already positive
       -- before it. The second clause is the one whose absence produced a
       -- non-monotonic curve the first time round.
       count(*) FILTER (
           WHERE p.last_observed_age >= g.checkpoint_seconds
              OR (p.first_positive_age IS NOT NULL
                  AND p.first_positive_age <= g.checkpoint_seconds)
       ) AS at_risk,
       -- An event first OBSERVED positive at 72 hours counts as reverted only
       -- from 72 hours. We know it had been reverted by then, not when. Interval
       -- censoring, resolved conservatively — the alternative is inventing a
       -- revert time nobody measured.
       count(*) FILTER (
           WHERE p.first_positive_age IS NOT NULL
             AND p.first_positive_age <= g.checkpoint_seconds
       ) AS reverted_by,
       -- Events actually OBSERVED at or beyond this age, positive or not.
       --
       -- Cumulative incidence is only defined where negatives have been seen
       -- at that age. Beyond the observation horizon a positive stays known
       -- forever while a negative needs a fresh look, so at_risk collapses to
       -- the positives alone and the rate reads 100% — which is what the
       -- 168-hour row said before this column existed to explain it.
       count(*) FILTER (WHERE p.last_observed_age >= g.checkpoint_seconds) AS observed_at_age,
       count(*) FILTER (WHERE p.cohort) AS cohort_rows
  FROM grid g
 CROSS JOIN per_event p
 GROUP BY g.checkpoint_seconds, COALESCE(p.stratum, 'unknown')
 ORDER BY g.checkpoint_seconds, stratum
"""


def _schema_state(conn: psycopg.Connection[Any]) -> dict[str, bool]:
    state: dict[str, bool] = {}
    for name, sql in SCHEMA_EXPECTATIONS.items():
        row = conn.execute(sql).fetchone()  # type: ignore[arg-type]
        state[name] = bool(row and row["present"])
    return state


def _connect() -> psycopg.Connection[Any]:
    settings = get_settings()
    conn = psycopg.connect(settings.serving_url, row_factory=psycopg.rows.dict_row)
    conn.read_only = True
    conn.execute("SET TIME ZONE 'UTC'")
    return conn


@app.get("/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    reachable = True
    detail: str | None = None
    schema: dict[str, bool] = {}
    try:
        with _connect() as conn:
            conn.execute("SELECT 1")
            schema = _schema_state(conn)
    except Exception as exc:  # noqa: BLE001
        reachable = False
        # The class name only. The message can contain the host, the user and,
        # on some drivers, the connection string itself.
        detail = type(exc).__name__

    behind = [name for name, present in schema.items() if not present]

    return {
        # A database missing a migration the deployed code depends on is not
        # healthy, even though every query in this endpoint still works.
        "status": "ok" if reachable and not behind else "degraded",
        "schema": schema,
        "schema_behind": behind,
        "env": settings.env,
        "env_is_valid": settings.env_is_valid,
        "build": settings.build_id,
        "database_reachable": reachable,
        "database_host": settings.serving_host,
        "readonly_role_in_use": settings.readonly_role_in_use,
        "error_class": detail,
    }


@app.get("/stats")
def stats() -> dict[str, Any]:
    """What the pipeline has accumulated, and how stale it is.

    Staleness is reported rather than left implied (NFR-11). A page showing
    counts with no indication of when they were last updated is indistinguishable
    from a page showing counts that stopped updating three days ago.
    """
    with _connect() as conn:
        totals = conn.execute(STATS_SQL).fetchone() or {}
        runs = conn.execute(RUNS_SQL).fetchall()
        mature = conn.execute(MATURE_SQL).fetchall()
        cover = conn.execute(COVERAGE_SQL).fetchone() or {}

    spanned = 0.0
    if totals.get("newest_event") and totals.get("oldest_event"):
        spanned = (totals["newest_event"] - totals["oldest_event"]).total_seconds()

    return {
        "coverage": {
            "spanned_hours": round(spanned / 3600, 2),
            "covered_hours": round(max(spanned - cover.get("missing_seconds", 0), 0) / 3600, 2),
            "gaps_over_10_min": cover.get("gap_count", 0),
            "largest_gap_minutes": round(cover.get("largest_gap_seconds", 0) / 60, 1),
            "largest_gap_from": cover.get("largest_gap_from"),
            "largest_gap_to": cover.get("largest_gap_to"),
        },
        "totals": {
            "events": totals.get("events", 0),
            "events_logged_out": totals.get("events_logged_out", 0),
            "tagged_at_ingest": totals.get("tagged_at_ingest", 0),
            "labels": totals.get("labels", 0),
            "label_checks": totals.get("label_checks", 0),
            "revert_events": totals.get("revert_events", 0),
            # M4 grades this cohort on a 48h window because it is the only
            # slice the labeller checks that early. If cohort_events is far
            # from a tenth of events, the cohort figure describes something
            # other than what it claims to.
            "cohort_events": totals.get("cohort_events", 0),
            # When the cohort starts. The flag is written at insert time and
            # rows inserted before that code shipped can never be corrected, so
            # the cohort covers events from here on rather than the whole
            # table — which is why cohort_events is far below a tenth.
            "first_cohort_event": totals.get("first_cohort_event"),
            "cohort_predictions": totals.get("cohort_predictions", 0),
            "newest_event": totals.get("newest_event"),
            "oldest_event": totals.get("oldest_event"),
        },
        # Rates only over matured edits, and always with their sample size.
        # A revert rate without a maturity is a lower bound, not a rate.
        "mature_48h": [
            {
                "stratum": row["sampling_stratum"],
                "n": row["n"],
                "reverted": row["reverted"],
                "rate": round(row["reverted"] / row["n"], 4) if row["n"] else None,
                # The same rate over the population the sample stands for.
                # Under a case-control frame these differ, and publishing only
                # one of them would mean choosing which.
                "weighted_n": round(float(row["weighted_n"] or 0)),
                "weighted_rate": (
                    round(float(row["weighted_reverted"] or 0) / float(row["weighted_n"]), 4)
                    if row["weighted_n"]
                    else None
                ),
            }
            for row in mature
        ],
        "runs": [
            {
                "job": row["job"],
                "status": row["status"],
                "last_run": row["started_at_utc"],
                "minutes_ago": row["minutes_ago"],
            }
            for row in runs
        ],
    }


@app.get("/maturity")
def maturity() -> dict[str, Any]:
    """Cumulative incidence of reverts by edit age, per stratum.

    The raw material for the maturity window (SRS §11, `PREREGISTRATION.md` §6).
    Published so the threshold that decides which predictions may count toward
    an accuracy figure can be recomputed rather than believed.

    `at_risk` counts only events that actually reached each age. `reverted_by`
    counts those whose FIRST positive check was at or before it. Neither is a
    per-checkpoint tally, and the difference is the whole point — see the
    comment on MATURITY_SQL.
    """
    with _connect() as conn:
        rows = conn.execute(MATURITY_SQL).fetchall()

    return {
        "checkpoints": [
            {
                "age_seconds": row["checkpoint_seconds"],
                "age_hours": round(row["checkpoint_seconds"] / 3600, 2),
                "stratum": row["stratum"],
                "at_risk": row["at_risk"],
                "reverted_by": row["reverted_by"],
                "cumulative_incidence": (
                    round(row["reverted_by"] / row["at_risk"], 5) if row["at_risk"] else None
                ),
                "observed_at_age": row["observed_at_age"],
                # A rate computed where almost nothing was observed at that age
                # is arithmetic, not measurement. 100 is a floor, not a claim
                # that 100 is enough.
                "reliable": row["observed_at_age"] >= 100,
                "cohort_rows": row["cohort_rows"],
            }
            for row in rows
        ],
        "note": (
            "Cumulative incidence by the age an observation was actually made, "
            "not by the checkpoint it was scheduled under. Rows with "
            "reliable=false have too few observations at that age for the rate "
            "to mean anything: beyond the observation horizon a positive stays "
            "known while a negative needs a fresh look, so the denominator "
            "collapses to the positives and the rate tends to 100%."
        ),
        "window_set": False,
        "window_blocked_on": (
            "A clean estimate needs the maturity cohort — events ingested live "
            "under the sampling frame and observed at every checkpoint as they "
            "age. Backfilled events were observed once, late, so they attribute "
            "three days of accumulated reverts to a single observation and "
            "cannot separate when a revert arrived from when we looked."
        ),
    }


@app.get("/kc2")
def kc2() -> dict[str, Any]:
    """The kill criterion, answered in public (M2 C-8).

    A criterion decided in a job log that needs a login to read is not a
    criterion; it is a private opinion with a number attached. Every evaluation
    appends a row, nothing may replace one, and this serves the latest.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM outcome.evaluations ORDER BY evaluated_at DESC LIMIT 1"
        ).fetchone()

    if not row:
        return {"decided": False, "note": "no evaluation has been run yet"}

    return {
        "decided": True,
        "clears_kc2": row["clears_kc2"],
        "provisional": row["provisional"],
        "margin": row["margin"],
        "margin_required": row["margin_required"],
        "ci": [row["ci_low"], row["ci_high"]],
        "pr_auc": row["pr_auc"],
        "evaluated_at": row["evaluated_at"],
        "window": [row["window_start"], row["window_end"]],
        "maturity_hours": row["maturity_hours"],
        "n_events": row["n_events"],
        "n_positives": row["n_positives"],
        "n_features": row["n_features"],
        # A model fitted to shuffled labels should score at the base rate.
        # Published beside the verdict so it can be judged against it.
        "null_pr_auc": row["null_pr_auc"],
        "base_rate": row["base_rate"],
        "feature_importance": row["feature_importance"],
        # Whether the verdict survives without its most important feature.
        "ablated_feature": row["ablated_feature"],
        "ablated_margin": row["ablated_margin"],
        "code_commit": row["code_commit"],
        "note": (
            "PROVISIONAL while maturity is fixed at 48h. The real window needs "
            "the maturity cohort to age; see /maturity."
            if row["provisional"]
            else "Final, against the estimated maturity window."
        ),
    }


REGISTER_SQL = """
SELECT count(*)                                                      AS predictions,
       count(DISTINCT model_version)                                 AS model_versions,
       count(*) FILTER (WHERE outcome_observable_at_scoring)         AS scored_late,
       min(scored_at)                                                AS first_score,
       max(scored_at)                                                AS latest_score,
       round((percentile_cont(0.5) WITHIN GROUP (
             ORDER BY EXTRACT(epoch FROM scored_at - event_ts)) / 60.0)::numeric, 1)
                                                                     AS lag_p50_minutes,
       round((percentile_cont(0.9) WITHIN GROUP (
             ORDER BY EXTRACT(epoch FROM scored_at - event_ts)) / 60.0)::numeric, 1)
                                                                     AS lag_p90_minutes,
       round((max(EXTRACT(epoch FROM scored_at - event_ts)) / 60.0)::numeric, 1)
                                                                     AS lag_max_minutes
  FROM register.predictions
 WHERE role = 'champion'
"""

# M5-FR-8. Shadow scores are reported as their own thing and never folded into
# the figures above. A challenger exists to be compared, not to be counted:
# adding its rows to the headline would double the prediction count and move
# the lag distribution for a model nobody is being served.
SHADOW_SQL = """
SELECT p.model_version,
       count(*)                                        AS predictions,
       min(p.scored_at)                                AS first_score,
       max(p.scored_at)                                AS latest_score,
       round(EXTRACT(epoch FROM now() - m.trained_at) / 86400.0, 2) AS shadow_days
  FROM register.predictions p
  JOIN register.model_registry m ON m.model_version = p.model_version
 WHERE p.role = 'shadow'
 GROUP BY p.model_version, m.trained_at
 ORDER BY max(p.scored_at) DESC
 LIMIT 1
"""

# The same question the scorer asked itself, asked again now.
#
# The stored flag is what the scorer KNEW at write time, and it can only ever
# understate: labels keep arriving after a score is written, and until this
# commit the scorer consulted only revert_events, so every prediction taken
# before it is flagged too low. register.predictions has no UPDATE grant — that
# is the point of it — so the row cannot be corrected in place. It is recomputed
# on read instead, against everything known now.
#
# This compares against p.scored_at rather than now(), which is the comparison
# that was actually wanted all along and is only available in hindsight: did we
# hold this answer at the moment we claimed to be predicting it?
REGISTER_RECHECK_SQL = """
SELECT count(*) AS known_before_scoring
  FROM register.predictions p
 WHERE p.role = 'champion'
   AND (EXISTS (SELECT 1 FROM outcome.revert_events r
                WHERE r.reverted_revid = p.revid AND r.revert_ts <= p.scored_at)
        OR EXISTS (SELECT 1 FROM outcome.labels l
                    WHERE l.revid = p.revid AND l.first_observed_at_utc <= p.scored_at))
"""

# M3-FR-18 publishes a rate, so the rate is served. The most recent run only:
# the table is append-only and every earlier run is still in it, but a status
# page that averages them would hide the one that just started failing.
REPRODUCTION_SQL = """
SELECT ran_at, window_start, window_end, sampled, hash_matched, score_matched,
       matched_at_scoring_time, unreproducible, state_predates_window,
       model_versions, code_commit
  FROM register.reproductions
 ORDER BY ran_at DESC
 LIMIT 1
"""

CHAMPION_SQL = """
SELECT model_version, trained_at, training_start, training_end, n_train_events,
       n_train_positives, artifact_path, artifact_sha256, offline_metrics,
       array_length(feature_names, 1) AS n_features, code_commit
  FROM register.model_registry
 ORDER BY trained_at DESC, model_version DESC
 LIMIT 1
"""


@app.get("/register")
def register_view() -> dict[str, Any]:
    """The prediction register, and the model that wrote to it (M3 D-4, D-5).

    Scoring lag is published as a DISTRIBUTION, not a target. This is a
    near-real-time system by choice (SRS §3.2), and a median with a p90 and a
    maximum says what that means far better than a number nobody meets.

    `scored_late` counts predictions written after their own outcome was
    already observable. Those are excluded from every accuracy claim: if the
    scorer falls far enough behind it would otherwise be marked correct for
    having been slow, which is the one way this project could improve its
    numbers by getting worse.
    """
    with _connect() as conn:
        totals = conn.execute(REGISTER_SQL).fetchone() or {}
        recheck = conn.execute(REGISTER_RECHECK_SQL).fetchone() or {}
        shadow = conn.execute(SHADOW_SQL).fetchone()
        reproduction = conn.execute(REPRODUCTION_SQL).fetchone()
        champion = conn.execute(CHAMPION_SQL).fetchone()

    scored = int(totals.get("predictions") or 0)
    late = int(totals.get("scored_late") or 0)
    known = int(recheck.get("known_before_scoring") or 0)

    def number(value: Any) -> float | None:
        """Postgres numeric arrives as Decimal, which serialises to a JSON
        STRING. A client parsing scoring_lag_minutes.p50 would get "20.0", and
        would either coerce it silently or compare it as text. Numbers are
        returned as numbers."""
        return None if value is None else float(value)

    return {
        "predictions": scored,
        "model_versions": totals.get("model_versions", 0),
        "first_score": totals.get("first_score"),
        "latest_score": totals.get("latest_score"),
        "scoring_lag_minutes": {
            "p50": number(totals.get("lag_p50_minutes")),
            "p90": number(totals.get("lag_p90_minutes")),
            "max": number(totals.get("lag_max_minutes")),
        },
        "scored_after_outcome_was_observable": {
            "count": known,
            "share": round(known / scored, 5) if scored else None,
            "flagged_by_the_scorer_at_write_time": late,
            "note": (
                "Excluded from every accuracy claim; see M3-FR-10. The headline "
                "count is recomputed on read against everything known now, and "
                "is the figure to use. The scorer's own flag is published beside "
                "it and can only understate: outcomes keep arriving after a "
                "score is written, and register.predictions cannot be updated by "
                "grant, so the row is never corrected in place."
            ),
        },
        "reproducibility": (
            None
            if not reproduction
            else {
                "ran_at": reproduction["ran_at"],
                "window": [reproduction["window_start"], reproduction["window_end"]],
                "sampled": reproduction["sampled"],
                "hash_matched": reproduction["hash_matched"],
                "score_matched": reproduction["score_matched"],
                "matched_only_at_scoring_time": reproduction["matched_at_scoring_time"],
                "unreproducible": reproduction["unreproducible"],
                "state_predates_window": reproduction["state_predates_window"],
                # Over what was checkable, and the denominator is published
                # beside it. A job that drops what it cannot verify reports a
                # clean rate over a shrinking base, which looks better every
                # time it gets worse.
                "agreement": (
                    round(
                        reproduction["hash_matched"]
                        / (reproduction["sampled"] - reproduction["state_predates_window"]),
                        5,
                    )
                    if reproduction["sampled"] - reproduction["state_predates_window"]
                    else None
                ),
                "checkable": reproduction["sampled"] - reproduction["state_predates_window"],
                "model_versions": reproduction["model_versions"],
                "note": (
                    "A sample is re-derived daily from the raw events and must produce "
                    "the same feature hash and the same score. matched_only_at_scoring_time "
                    "counts predictions that reproduce under the state the SCORER held "
                    "rather than the state training would have built — the gap between "
                    "them is scoring lag, and it is a measurement, not a failure. Only the "
                    "hash is stored, not the vector, so anything in unreproducible says "
                    "something differs without saying what."
                ),
            }
        ),
        # Published, and published apart. P-4 requires seven days of wall-clock
        # shadow, so how long this has been running is the number a reader needs
        # to know how far off a decision is.
        "shadow": (
            None
            if not shadow
            else {
                "model_version": shadow["model_version"],
                "predictions": shadow["predictions"],
                "first_score": shadow["first_score"],
                "latest_score": shadow["latest_score"],
                "shadow_days": float(shadow["shadow_days"]),
                "note": (
                    "A challenger scoring alongside the champion. Never served, and "
                    "excluded from every figure above. Promotion requires the "
                    "pre-registered conditions in PREREGISTRATION.md section 5 — see "
                    "/decisions."
                ),
            }
        ),
        "champion": (
            None
            if not champion
            else {
                "model_version": champion["model_version"],
                "trained_at": champion["trained_at"],
                "training_window": [champion["training_start"], champion["training_end"]],
                "n_train_events": champion["n_train_events"],
                "n_train_positives": champion["n_train_positives"],
                "n_features": champion["n_features"],
                "artifact_path": champion["artifact_path"],
                "artifact_sha256": champion["artifact_sha256"],
                "offline_metrics": champion["offline_metrics"],
                "code_commit": champion["code_commit"],
                "note": (
                    "The artifact is in git at artifact_path. Its sha256 is "
                    "verified before every load, so a mismatch refuses to score "
                    "rather than scoring differently."
                ),
            }
        ),
    }


# --- M4: continuous evaluation ---------------------------------------------

# The most recent run only. Every earlier run is still in the table — it is
# append-only — but a page that averaged them would smooth over the moment a
# number started moving, which is the only moment worth catching.
# The most recent Lift Wing attempt, published whatever it says. M4-FR-21: a
# gated or unavailable service is an unmet dependency, not a reason to quietly
# substitute a comparator that happens to be reachable.
LIFTWING_ATTEMPT_SQL = """
SELECT attempted_at, requested, fetched, status, detail
  FROM outcome.liftwing_attempts
 ORDER BY attempted_at DESC
 LIMIT 1
"""

LATEST_METRICS_SQL = """
WITH latest AS (SELECT max(computed_at) AS at FROM outcome.prediction_metrics)
SELECT m.*
  FROM outcome.prediction_metrics m, latest
 WHERE m.computed_at = latest.at
 ORDER BY m.population, m.window_label, m.segment, m.segment_level
"""

CALIBRATION_SQL = """
SELECT b.bin_index, b.bin_low, b.bin_high, b.n, b.mean_predicted,
       b.observed_rate, b.weighted_observed_rate, m.window_label, m.computed_at,
       m.maturity_hours, m.provisional, m.n AS window_n
  FROM outcome.calibration_bins b
  JOIN outcome.prediction_metrics m ON m.metric_id = b.metric_id
 WHERE m.computed_at = (SELECT max(computed_at) FROM outcome.prediction_metrics)
   AND m.window_label = %(window)s
   AND m.population = %(population)s
   AND m.segment = 'all'
 ORDER BY b.bin_index
"""


def _metric_row(row: dict[str, Any]) -> dict[str, Any]:
    """Every metric carries `n`, an interval, and the maturity window it used
    (M4-FR-1). A point estimate published alone invites a conclusion the early
    cohorts cannot support."""
    return {
        "segment": row["segment"],
        "level": row["segment_level"],
        "n": row["n"],
        "n_positives": row["n_positives"],
        "base_rate": row["base_rate"],
        "weighted_base_rate": row["weighted_base_rate"],
        "pr_auc": row["pr_auc"],
        "pr_auc_ci": [row["pr_auc_ci_low"], row["pr_auc_ci_high"]],
        "roc_auc": row["roc_auc"],
        "brier": row["brier"],
        "baseline_pr_auc": row["baseline_pr_auc"],
        "margin": row["margin"],
        "margin_ci": [row["margin_ci_low"], row["margin_ci_high"]],
        # The institutional benchmark, on the paired subset only. liftwing_n is
        # smaller than n because Lift Wing is sampled, and model_pr_auc_on_paired
        # is Bellwether's figure over exactly those events — NOT pr_auc, which
        # covers the whole window. Comparing pr_auc against liftwing_pr_auc
        # would set two populations against each other and call it a margin.
        "liftwing": {
            "n": row["liftwing_n"],
            "liftwing_pr_auc": row["liftwing_pr_auc"],
            "model_pr_auc_on_paired": row["model_pr_auc_on_paired"],
            "margin": row["liftwing_margin"],
            "margin_ci": [row["liftwing_margin_ci_low"], row["liftwing_margin_ci_high"]],
            "note": "positive margin means Bellwether ahead; SRS 6.4 predicted it would not be",
        },
    }


@app.get("/metrics")
def metrics_view() -> dict[str, Any]:
    """Live performance on the predictions this system actually made (M4-FR-22).

    Not the same thing as `/kc2`, and the difference is the point. `/kc2` is an
    offline backtest over a backfilled census; this grades forecasts committed
    to an append-only register before the answer existed. Where they disagree,
    this one is true.
    """
    with _connect() as conn:
        rows = conn.execute(LATEST_METRICS_SQL).fetchall()
        attempt = conn.execute(LIFTWING_ATTEMPT_SQL).fetchone()

    if not rows:
        return {
            "computed": False,
            "note": (
                "No metrics yet. The register is younger than the maturity window, "
                "so nothing in it can be graded."
            ),
        }

    populations: dict[str, Any] = {}
    for row in rows:
        windows = populations.setdefault(
            row["population"], {"maturity_hours": row["maturity_hours"], "windows": {}}
        )["windows"]
        bucket = windows.setdefault(
            row["window_label"],
            {
                "window": [row["window_start"], row["window_end"]],
                "maturity_hours": row["maturity_hours"],
                "provisional": row["provisional"],
                # M4-FR-2: exclusions are published, never applied silently.
                # excluded_late_base_rate is the one to read — those rows are
                # not a random sample, they concentrate in edits reverted fast,
                # so the exclusion selects on the outcome it is protecting.
                "excluded": {
                    "immature": row["excluded_immature"],
                    "late": row["excluded_late"],
                    "late_base_rate": row["excluded_late_base_rate"],
                },
                "aggregate": None,
                "segments": [],
            },
        )
        if row["segment"] == "all":
            bucket["aggregate"] = _metric_row(row)
        else:
            bucket["segments"].append(_metric_row(row))

    return {
        "computed": True,
        "computed_at": rows[0]["computed_at"],
        "code_commit": rows[0]["code_commit"],
        "populations": populations,
        "liftwing_last_attempt": (
            None
            if not attempt
            else {
                "attempted_at": attempt["attempted_at"],
                "requested": attempt["requested"],
                "fetched": attempt["fetched"],
                "status": attempt["status"],
                "detail": attempt["detail"],
            }
        ),
        "note": (
            "Live figures on register predictions. Distinct from /kc2, which is an "
            "offline backtest over a backfilled census — the two measure different "
            "populations and neither replaces the other. Segments are diagnosis and "
            "are never a headline result (M4-FR-14). "
            "TWO populations are published and they are not interchangeable: 'all' is "
            "every scored event matured at seven days, because outside the maturity "
            "cohort the labeller checks exactly once and does so at the seven-day "
            "checkpoint; 'maturity_cohort' is the 10% receiving the full grid, matured "
            "at 48 hours, arriving five days sooner over a tenth as many events. The "
            "cohort is a deterministic bucket keyed on revid, so within the events it "
            "covers it is smaller rather than skewed — but it covers events from "
            "first_cohort_event onward only, because the flag is written at insert time "
            "and earlier rows cannot be corrected. Neither population substitutes for "
            "the other."
        ),
    }


@app.get("/calibration")
def calibration_view(window: str = "7d", population: str = "all") -> dict[str, Any]:
    """Whether a score of 0.9 means what it says (M4-FR-23).

    Both rates are served. The frame keeps 50% of logged-out edits and 3% of
    registered ones, so the raw sample frequency is around four times the
    population's — a model calibrated against it would be calibrated to a
    population that does not exist and would overstate risk in production by
    roughly that factor.
    """
    with _connect() as conn:
        rows = conn.execute(
            CALIBRATION_SQL, {"window": window, "population": population}
        ).fetchall()

    if not rows:
        return {
            "computed": False,
            "window": window,
            "population": population,
            "note": "No calibration curve yet.",
        }

    return {
        "computed": True,
        "window": window,
        "population": population,
        "computed_at": rows[0]["computed_at"],
        "maturity_hours": rows[0]["maturity_hours"],
        "provisional": rows[0]["provisional"],
        "n": rows[0]["window_n"],
        "bins": [
            {
                "range": [b["bin_low"], b["bin_high"]],
                "n": b["n"],
                "mean_predicted": b["mean_predicted"],
                "observed_rate": b["observed_rate"],
                "weighted_observed_rate": b["weighted_observed_rate"],
            }
            for b in rows
        ],
        "note": (
            "Equal-width bins, not quantiles: the question is whether 0.9 means what "
            "it says, and quantile edges would move every run so two runs could not be "
            "compared. Empty bins are published — a bin holding four events and one "
            "holding four thousand look identical without n. Use weighted_observed_rate "
            "for anything facing production."
        ),
    }


# --- M5: the decision log --------------------------------------------------

# Every decision, whole (M5-FR-32).
#
# Including rejections, and deliberately not paginated or filtered by outcome.
# A log that serves promotions by default answers "what changed" and hides "what
# was considered and refused", which is the question that shows the rule binding
# rather than being satisfied by everything that reached it.
DECISIONS_SQL = """
SELECT decision_id, decided_at, decision, champion_version, challenger_version,
       trigger_reason,
       p1_pr_auc_gain, p1_pass, p2_ci_low, p2_ci_high, p2_pass,
       p3_matured_positives, p3_pass, p4_shadow_days, p4_pass,
       p5_ece_regression, p5_worst_segment, p5_worst_segment_regression, p5_pass,
       champion_pr_auc, challenger_pr_auc, n_matured, n_positives,
       prereg_commit, code_commit
  FROM decide.model_decisions
 ORDER BY decided_at DESC
"""

CHAMPION_HISTORY_SQL = """
SELECT model_version, effective_from, replaced, decision_id
  FROM decide.champion_history
 ORDER BY effective_from DESC, history_id DESC
 LIMIT 20
"""

# The evaluations that decide whether a challenger exists at all. Included
# because a decision log without them answers "why was this promoted" and not
# "why was anything trained", and the second question is where an unattended
# system is least explicable after the fact.
RECENT_TRIGGERS_SQL = """
SELECT window_day, champion_version, rolling_pr_auc, baseline_pr_auc, pr_auc_drop,
       max_psi, max_psi_feature, days_since_train, decay_breached, drift_breached,
       floor_breached, decay_streak, drift_streak, fired, fired_reason, n_matured
  FROM decide.trigger_evaluations
 ORDER BY window_day DESC
 LIMIT 14
"""


def _decision(row: dict[str, Any]) -> dict[str, Any]:
    """One decision, reconstructible from this alone (M5-FR-31).

    Every condition's measured value and verdict travels with it. A decision
    that needs the database to interpret is one only its owner can check, which
    is the standard the metric card, the artifact digest in git and the monthly
    seal are all held to.
    """
    return {
        "decision_id": row["decision_id"],
        "decided_at": row["decided_at"],
        "decision": row["decision"],
        "champion": row["champion_version"],
        "challenger": row["challenger_version"],
        "reason": row["trigger_reason"],
        "conditions": {
            "P-1": {
                "what": "PR-AUC gain over the champion, on the same matured events",
                "measured": row["p1_pr_auc_gain"],
                "pass": row["p1_pass"],
            },
            "P-2": {
                "what": "paired bootstrap 95% interval excludes zero",
                "measured": [row["p2_ci_low"], row["p2_ci_high"]],
                "pass": row["p2_pass"],
            },
            "P-3": {
                "what": "matured positives accumulated in shadow",
                "measured": row["p3_matured_positives"],
                "pass": row["p3_pass"],
            },
            "P-4": {
                "what": "wall-clock days of shadow running",
                "measured": row["p4_shadow_days"],
                "pass": row["p4_pass"],
            },
            "P-5": {
                "what": "calibration and no segment regression",
                "ece_regression": row["p5_ece_regression"],
                "worst_segment": row["p5_worst_segment"],
                "worst_segment_regression": row["p5_worst_segment_regression"],
                "pass": row["p5_pass"],
            },
        },
        "champion_pr_auc": row["champion_pr_auc"],
        "challenger_pr_auc": row["challenger_pr_auc"],
        "n_matured": row["n_matured"],
        "n_positives": row["n_positives"],
        "code_commit": row["code_commit"],
    }


@app.get("/decisions")
def decisions_view() -> dict[str, Any]:
    """What this system decided about itself, and why (M5-FR-32).

    The point of M5 is a model that maintains itself unattended. The only thing
    that makes that defensible rather than alarming is that the rule was written
    down before any model existed and every decision it makes is recorded with
    the evidence that produced it — including the ones where it refused.
    """
    with _connect() as conn:
        decisions = conn.execute(DECISIONS_SQL).fetchall()
        history = conn.execute(CHAMPION_HISTORY_SQL).fetchall()
        evaluations = conn.execute(RECENT_TRIGGERS_SQL).fetchall()

    counts = {"promote": 0, "reject": 0, "rollback": 0}
    for row in decisions:
        counts[row["decision"]] = counts.get(row["decision"], 0) + 1

    return {
        "serving": history[0]["model_version"] if history else None,
        "counts": counts,
        "decisions": [_decision(row) for row in decisions],
        "champion_history": [
            {
                "model_version": row["model_version"],
                "effective_from": row["effective_from"],
                "replaced": row["replaced"],
                "decision_id": row["decision_id"],
            }
            for row in history
        ],
        "recent_trigger_evaluations": [
            {
                "day": row["window_day"],
                "champion": row["champion_version"],
                "rolling_pr_auc": row["rolling_pr_auc"],
                "baseline_pr_auc": row["baseline_pr_auc"],
                "drop": row["pr_auc_drop"],
                "worst_psi": row["max_psi"],
                "worst_psi_feature": row["max_psi_feature"],
                "days_since_train": row["days_since_train"],
                "breached": {
                    "decay": row["decay_breached"],
                    "drift": row["drift_breached"],
                    "floor": row["floor_breached"],
                },
                "streaks": {"decay": row["decay_streak"], "drift": row["drift_streak"]},
                "fired": row["fired"],
                "reason": row["fired_reason"],
                "n_matured": row["n_matured"],
            }
            for row in evaluations
        ],
        "note": (
            "Every decision, including rejections, which are the more informative "
            "rows: a log holding only promotions answers what changed and cannot "
            "answer what was considered and refused. Each carries every condition's "
            "measured value and verdict, so it can be checked without database "
            "access. The conditions themselves were fixed in PREREGISTRATION.md "
            "before the first model was trained, and the constants are asserted "
            "against that document by the test suite. Trigger evaluations are "
            "included because a decision log without them explains why a model was "
            "promoted but not why anything was trained."
        ),
    }


# --- M6: authentication ----------------------------------------------------
#
# The endpoints below are the only ones that can write anything, and they write
# to two tables: human labels and the audit log. Everything else in this file
# reads.


@app.post("/auth/sign-in")
def sign_in_endpoint(
    request: Request, response: Response, credentials: sessions.Credentials
) -> dict[str, Any]:
    return sessions.sign_in(request, response, credentials)


@app.post("/auth/sign-out")
def sign_out_endpoint(request: Request, response: Response) -> dict[str, str]:
    return sessions.sign_out(request, response)


@app.get("/auth/me")
def me_endpoint(request: Request) -> dict[str, Any]:
    """Who the browser is, if anyone.

    200 with `authenticated: false` rather than a 401, because "nobody is
    signed in" is the ordinary state of a public page and not an error the
    frontend should render as a failure.
    """
    user = sessions.current_user(request.cookies.get(sessions.SESSION_COOKIE))
    if not user:
        return {"authenticated": False, "auth_configured": get_settings().app_role_configured}
    return {
        "authenticated": True,
        "email": user["email"],
        "role": user["role"],
        "auth_configured": True,
    }
