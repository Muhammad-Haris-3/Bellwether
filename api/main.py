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

MATURE_SQL = """
SELECT sampling_stratum,
       count(*) AS n,
       count(*) FILTER (WHERE 'mw-reverted' = ANY(tags)) AS reverted
  FROM landing.rc_events
 WHERE now() - event_ts >= interval '48 hours'
 GROUP BY sampling_stratum
 ORDER BY sampling_stratum
"""


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
    try:
        with _connect() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        reachable = False
        # The class name only. The message can contain the host, the user and,
        # on some drivers, the connection string itself.
        detail = type(exc).__name__

    return {
        "status": "ok" if reachable else "degraded",
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

    return {
        "totals": {
            "events": totals.get("events", 0),
            "events_logged_out": totals.get("events_logged_out", 0),
            "reverted": totals.get("reverted", 0),
            "labels": totals.get("labels", 0),
            "label_checks": totals.get("label_checks", 0),
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
