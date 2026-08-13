"""Ingestion (M0-T2, FR-1 to FR-6).

Reads main-namespace non-bot edits forward from a durable cursor and writes
them to ``landing.rc_events``.

Three properties matter more than throughput:

  * **No loss.** The cursor advances only after the rows it covers are
    committed. A crashed, cancelled or never-scheduled run becomes latency,
    never a hole. GitHub's cron is best-effort and this is the design that
    makes that acceptable rather than a defect to apologise for.

  * **No duplicates.** Enforced by the primary key on ``revid`` and
    ``ON CONFLICT DO NOTHING``, not by application logic. Idempotency that
    depends on the caller behaving correctly is not idempotency.

  * **Bounded runs.** ``max_pages_per_run`` keeps one execution inside the
    workflow's ten-minute budget. Hitting the cap is a normal outcome: the
    cursor is left where it got to and the next run continues.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from bellwether.config import get_settings
from bellwether.db import advisory_lock, connect
from bellwether.http import MediaWikiClient
from bellwether.mediawiki import iter_recent_changes
from bellwether.runlog import RunContext, new_run_id, utcnow

JOB = "ingest"

# Arbitrary but fixed. Only needs to be distinct from other jobs' keys.
INGEST_LOCK_KEY = 815_001

INSERT_SQL = """
INSERT INTO landing.rc_events (
    revid, old_revid, rcid, event_ts, ns, title,
    user_name, user_id, is_anon, is_temp, is_minor, is_bot,
    comment, comment_hidden, user_hidden, oldlen, newlen, tags,
    sampling_stratum, sampling_weight, ingested_at_utc, ingest_run_id
) VALUES (
    %(revid)s, %(old_revid)s, %(rcid)s, %(event_ts)s, %(ns)s, %(title)s,
    %(user_name)s, %(user_id)s, %(is_anon)s, %(is_temp)s, %(is_minor)s, %(is_bot)s,
    %(comment)s, %(comment_hidden)s, %(user_hidden)s, %(oldlen)s, %(newlen)s, %(tags)s,
    %(sampling_stratum)s, %(sampling_weight)s, %(ingested_at_utc)s, %(ingest_run_id)s
)
ON CONFLICT (revid) DO NOTHING
"""


def stratum_of(event: dict[str, Any]) -> str:
    """Which sampling stratum an event belongs to (SRS 6.3).

    The dividing line is *logged out* versus *logged in*, not *IP* versus
    *account*. English Wikipedia masks IPs behind temporary accounts, so a
    logged-out editor now arrives as a named account with `temp` set. A frame
    keyed on `anon` alone would put every edit in the registered stratum and
    sample away the population the model most needs to see.

    M0 ingests both strata at 100%, so the weight is 1.0 throughout. The label
    is recorded anyway: M1 sets the registered-user sampling rate from the
    volume M0 measures, and weights recorded at observation time are worth more
    than weights reconstructed afterwards from a rule that may have changed.
    """
    return "logged_out" if (event["is_anon"] or event.get("is_temp")) else "registered"


def read_cursor(conn: Any) -> datetime | None:
    row = conn.execute("SELECT position_utc FROM landing.cursors WHERE job = %s", (JOB,)).fetchone()
    return row["position_utc"] if row else None


def write_cursor(conn: Any, position: datetime, run_id: Any) -> None:
    conn.execute(
        """
        INSERT INTO landing.cursors (job, position_utc, updated_at_utc, updated_by_run)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (job) DO UPDATE
           SET position_utc   = EXCLUDED.position_utc,
               updated_at_utc = EXCLUDED.updated_at_utc,
               updated_by_run = EXCLUDED.updated_by_run
        """,
        (JOB, position, utcnow(), run_id),
    )


def insert_page(conn: Any, events: list[dict[str, Any]], run_id: Any) -> int:
    """Insert one page of events. Returns how many were new."""
    if not events:
        return 0

    now = utcnow()
    rows = [
        {
            **event,
            "sampling_stratum": stratum_of(event),
            "sampling_weight": 1.0,
            "ingested_at_utc": now,
            "ingest_run_id": run_id,
        }
        for event in events
    ]

    with conn.cursor() as cur:
        cur.executemany(INSERT_SQL, rows)
        # psycopg3 accumulates affected rows across an executemany, so this is
        # the count of rows that were genuinely new — duplicates suppressed by
        # ON CONFLICT contribute zero. That distinction is the whole point of
        # the idempotency test.
        return max(cur.rowcount, 0)


def run(*, start_override: datetime | None = None, max_pages: int | None = None) -> dict[str, Any]:
    settings = get_settings()
    run_id = new_run_id()
    pages = max_pages if max_pages is not None else settings.max_pages_per_run

    with connect() as lock_conn, advisory_lock(lock_conn, INGEST_LOCK_KEY) as acquired:
        if not acquired:
            # Not an error. A previous run is still going; the next scheduled
            # tick will pick up wherever it leaves off.
            print("ingest: another run holds the lock, exiting cleanly")
            return {"skipped": True, "reason": "locked"}

        with connect() as conn:
            cursor = start_override or read_cursor(conn)
            if cursor is None:
                cursor = utcnow() - timedelta(minutes=settings.cold_start_lookback_minutes)
                print(f"ingest: no cursor, cold-starting from {cursor.isoformat()}")

        with RunContext(run_id, job=JOB, window_from=cursor) as run_ctx:
            newest = cursor
            with MediaWikiClient() as client:
                for page in iter_recent_changes(client, start=cursor, max_pages=pages):
                    # One transaction per page: commit the rows, then move the
                    # cursor. Doing it the other way round would lose a page on
                    # a crash between the two.
                    with connect() as conn:
                        written = insert_page(conn, page, run_id)
                        page_newest = max(e["event_ts"] for e in page)
                        if page_newest > newest:
                            newest = page_newest
                        write_cursor(conn, newest, run_id)

                    run_ctx.rows_read += len(page)
                    run_ctx.rows_written += written

                run_ctx.api_calls = client.calls

            run_ctx.window_to = newest
            result = {
                "run_id": str(run_id),
                "from": cursor.isoformat(),
                "to": newest.isoformat(),
                "read": run_ctx.rows_read,
                "written": run_ctx.rows_written,
                "api_calls": run_ctx.api_calls,
            }

    print(
        f"ingest: {result['read']} read, {result['written']} new, "
        f"{result['api_calls']} API calls, cursor at {result['to']}"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        help="ISO timestamp to start from, ignoring the stored cursor. "
        "For backfills and for the deliberate-outage test in M0-T7.",
    )
    parser.add_argument("--max-pages", type=int, default=None)
    args = parser.parse_args()

    start = None
    if args.since:
        start = datetime.fromisoformat(args.since.replace("Z", "+00:00")).astimezone(UTC)

    run(start_override=start, max_pages=args.max_pages)
    return 0


if __name__ == "__main__":
    sys.exit(main())
