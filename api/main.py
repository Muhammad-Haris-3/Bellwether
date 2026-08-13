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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bellwether.config import get_settings

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
    (SELECT count(*) FROM landing.rc_events WHERE 'mw-reverted' = ANY(tags)) AS reverted,
    (SELECT count(*) FROM outcome.labels)                                   AS labels,
    (SELECT count(*) FROM outcome.label_checks)                             AS label_checks,
    -- Reverting edits are recorded for the whole feed, outside the sampling
    -- frame (M1 6.1). Published because the ratio between this and `events` is
    -- the check that the frame has not silently blinded the secondary label
    -- path — the failure it was introduced to prevent.
    (SELECT count(*) FROM outcome.revert_events)                            AS revert_events,
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
MATURE_SQL = """
SELECT sampling_stratum,
       count(*)                                          AS n,
       count(*) FILTER (WHERE 'mw-reverted' = ANY(tags)) AS reverted,
       sum(sampling_weight)                              AS weighted_n,
       sum(sampling_weight) FILTER (WHERE 'mw-reverted' = ANY(tags)) AS weighted_reverted
  FROM landing.rc_events
 WHERE now() - event_ts >= interval '48 hours'
 GROUP BY sampling_stratum
 ORDER BY sampling_stratum
"""


# What each migration is expected to have left behind.
#
# The pipeline deploys on every push; the schema is applied by hand through
# bootstrap_database.py. So code can be ahead of the database, and the symptom
# is a column that does not exist — reported by a failing job, in a log, some
# time later. This makes the question "which migration does this database
# actually have" answerable from outside, in one request.
SCHEMA_EXPECTATIONS = {
    "001_schema": (
        "SELECT to_regclass('landing.rc_events') IS NOT NULL"
        "   AND to_regclass('outcome.labels') IS NOT NULL AS present"
    ),
    "002_roles": (
        "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bellwether_writer')"
        "   AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bellwether_readonly')"
        "    AS present"
    ),
    "003_m1_frame": (
        "SELECT to_regclass('landing.tag_names') IS NOT NULL"
        "   AND EXISTS (SELECT 1 FROM information_schema.columns"
        "                WHERE table_schema = 'landing' AND table_name = 'rc_events'"
        "                  AND column_name = 'tag_ids')"
        "   AND EXISTS (SELECT 1 FROM information_schema.columns"
        "                WHERE table_schema = 'landing' AND table_name = 'rc_events'"
        "                  AND column_name = 'in_maturity_cohort') AS present"
    ),
    "004_m1_gaps": ("SELECT to_regclass('landing.gap_attempts') IS NOT NULL AS present"),
    "006_m1_revert_events": ("SELECT to_regclass('outcome.revert_events') IS NOT NULL AS present"),
    "005_m1_retention": (
        "SELECT to_regclass('outcome.seals') IS NOT NULL"
        "   AND EXISTS (SELECT 1 FROM pg_proc p"
        "                 JOIN pg_namespace n ON n.oid = p.pronamespace"
        "                WHERE n.nspname = 'landing' AND p.proname = 'prune_expired')"
        "   AND EXISTS (SELECT 1 FROM information_schema.columns"
        "                WHERE table_schema = 'outcome' AND table_name = 'label_checks'"
        "                  AND column_name = 'in_maturity_cohort') AS present"
    ),
}


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
#   * The denominator is events that actually reached that age, not all events.
#     Counting a two-hour-old edit against the 48-hour checkpoint would treat
#     "has not had time" as "was not reverted", which is precisely the
#     right-censoring the whole maturity exercise exists to handle.
MATURITY_SQL = """
WITH per_event AS (
    SELECT c.revid,
           max(c.checkpoint_seconds)                                  AS reached,
           min(c.checkpoint_seconds) FILTER (WHERE c.had_reverted_tag) AS first_positive,
           bool_or(c.in_maturity_cohort)                              AS cohort,
           max(e.sampling_stratum)                                    AS stratum
      FROM outcome.label_checks c
      LEFT JOIN landing.rc_events e ON e.revid = c.revid
     GROUP BY c.revid
),
grid AS (SELECT DISTINCT checkpoint_seconds FROM outcome.label_checks)
SELECT g.checkpoint_seconds,
       COALESCE(p.stratum, 'unknown')                        AS stratum,
       count(*) FILTER (WHERE p.reached >= g.checkpoint_seconds)          AS at_risk,
       count(*) FILTER (WHERE p.reached >= g.checkpoint_seconds
                          AND p.first_positive IS NOT NULL
                          AND p.first_positive <= g.checkpoint_seconds)   AS reverted_by,
       count(*) FILTER (WHERE p.cohort)                                    AS cohort_rows
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
            "reverted": totals.get("reverted", 0),
            "labels": totals.get("labels", 0),
            "label_checks": totals.get("label_checks", 0),
            "revert_events": totals.get("revert_events", 0),
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
                "cohort_rows": row["cohort_rows"],
            }
            for row in rows
        ],
        "note": (
            "Cumulative incidence, not a per-checkpoint rate. An event reverted "
            "by one hour is still reverted at six, but is no longer re-checked, "
            "so per-checkpoint tallies under-count later ages."
        ),
    }
