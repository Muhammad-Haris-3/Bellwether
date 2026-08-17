"""Primary label harvest (M0-T4, FR-7, FR-9, FR-10).

Re-checks previously ingested edits for the ``mw-reverted`` tag at a fixed grid
of ages, and records **every** check — including the ones that find nothing.

The negatives are the point. "Was it reverted" is answerable from the positives
alone; "how long does it take to find out" is not, and that second question is
what sets the maturity window in M2. A check that found nothing at four hours
is an observation, not a wasted request.

Two things this job deliberately does not do:

  * It does not re-check an edit whose revert has already been observed. The
    outcome is known and asking again spends someone else's bandwidth to learn
    nothing. A revert that is itself later reverted would make this wrong; that
    is recorded as a known limitation rather than guessed at, because measuring
    how often it happens is cheaper than defending an assumption about it.

  * It does not write a negative label before the final checkpoint. An edit
    that has not been reverted yet is censored, not negative, and collapsing
    the two is the single easiest way to publish an accuracy figure that is
    quietly wrong.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from bellwether.config import LABEL_CHECKPOINTS_SECONDS, get_settings
from bellwether.db import advisory_lock, connect
from bellwether.http import MediaWikiClient
from bellwether.mediawiki import REVERTED_TAG, fetch_revision_tags
from bellwether.runlog import RunContext, new_run_id, utcnow
from bellwether.usage import record_on_exit

JOB = "label"
LABEL_LOCK_KEY = 815_002

# The last checkpoint is where an unreverted edit is finally called negative.
# It is a placeholder for the maturity window, which M2 estimates from the
# survival curve this job is collecting. Named rather than inlined so that
# changing it is a decision someone has to make on purpose.
FINAL_CHECKPOINT = max(LABEL_CHECKPOINTS_SECONDS)

DUE_SQL = """
WITH checkpoints AS (
    SELECT unnest(%(checkpoints)s::bigint[]) AS checkpoint_seconds
)
SELECT e.revid,
       e.event_ts,
       c.checkpoint_seconds,
       EXTRACT(epoch FROM now() - e.event_ts)::bigint AS age_seconds
  FROM landing.rc_events e
 CROSS JOIN checkpoints c
 -- The full grid runs on the maturity cohort only (M1 section 5). Everything
 -- else gets exactly one check, at the final checkpoint, which is all
 -- production needs: the grid exists to estimate one survival curve in M2, and
 -- estimating it from one tenth of a probability sample is a study, not a
 -- shortcut. Five rows an event was the largest storage line after rc_events.
 --
 -- No per-cent sign anywhere in this string. psycopg reads a bare percent as
 -- the start of a placeholder, including inside a SQL comment, and the error
 -- it raises names neither the comment nor the line.
 WHERE (e.in_maturity_cohort OR c.checkpoint_seconds = %(final)s)
   AND EXTRACT(epoch FROM now() - e.event_ts) >= c.checkpoint_seconds
   AND NOT EXISTS (
           SELECT 1 FROM outcome.label_checks lc
            WHERE lc.revid = e.revid
              AND lc.checkpoint_seconds = c.checkpoint_seconds)
   AND NOT EXISTS (
           SELECT 1 FROM outcome.labels l
            WHERE l.revid = e.revid
              AND l.label_source = 'mw_reverted'
              AND l.label)
 -- Least overdue first. A check nominally due at one hour but performed at
 -- five records an age of five, which is honest but degrades the grid the
 -- survival estimate is built on. Prioritising by overdueness keeps the grid
 -- as close to its intended shape as the request budget allows.
 ORDER BY (EXTRACT(epoch FROM now() - e.event_ts)::bigint - c.checkpoint_seconds) ASC
 LIMIT %(limit)s
"""

INSERT_CHECK_SQL = """
INSERT INTO outcome.label_checks
    (revid, checkpoint_seconds, checked_at_utc, age_seconds,
     had_reverted_tag, rev_missing, run_id)
VALUES (%(revid)s, %(checkpoint_seconds)s, %(checked_at_utc)s, %(age_seconds)s,
        %(had_reverted_tag)s, %(rev_missing)s, %(run_id)s)
ON CONFLICT (revid, checkpoint_seconds) DO NOTHING
"""

INSERT_LABEL_SQL = """
INSERT INTO outcome.labels
    (revid, label, label_source, first_observed_at_utc,
     detection_latency_seconds, observed_run_id)
VALUES (%(revid)s, %(label)s, 'mw_reverted', %(first_observed_at_utc)s,
        %(detection_latency_seconds)s, %(run_id)s)
ON CONFLICT (revid, label_source) DO NOTHING
"""


def due_checks(conn: Any, limit: int) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            DUE_SQL,
            {
                "checkpoints": list(LABEL_CHECKPOINTS_SECONDS),
                "final": FINAL_CHECKPOINT,
                "limit": limit,
            },
        )
        return cur.fetchall()


def run(*, limit: int | None = None) -> dict[str, Any]:
    """Check every edge due a checkpoint, up to ``limit`` (revid, checkpoint) pairs."""
    settings = get_settings()
    run_id = new_run_id()
    limit = limit if limit is not None else settings.max_label_checks_per_run

    with connect() as lock_conn, advisory_lock(lock_conn, LABEL_LOCK_KEY) as acquired:
        if not acquired:
            print("label: another run holds the lock, exiting cleanly")
            return {"skipped": True, "reason": "locked"}

        with connect() as conn:
            due = due_checks(conn, limit)

        if not due:
            print("label: nothing due")
            return {"due": 0, "checked": 0, "reverted": 0}

        # One request per revid, however many checkpoints it is due at. A
        # single observation answers all of them, and the age recorded on each
        # row is the true age at observation, not the checkpoint's nominal one.
        by_revid: dict[int, list[dict[str, Any]]] = {}
        for row in due:
            by_revid.setdefault(int(row["revid"]), []).append(row)

        revids = list(by_revid)
        found_positive = 0
        missing_count = 0

        with RunContext(run_id, job=JOB) as run_ctx:
            with MediaWikiClient() as client:
                found, missing = fetch_revision_tags(client, revids)
                run_ctx.api_calls = client.calls

            checked_at = utcnow()
            check_rows: list[dict[str, Any]] = []
            label_rows: list[dict[str, Any]] = []

            for revid, pending in by_revid.items():
                is_missing = revid in missing
                tags = found.get(revid, {}).get("tags", [])
                reverted = REVERTED_TAG in tags

                if is_missing:
                    missing_count += 1
                if reverted:
                    found_positive += 1

                for row in pending:
                    check_rows.append(
                        {
                            "revid": revid,
                            "checkpoint_seconds": row["checkpoint_seconds"],
                            "checked_at_utc": checked_at,
                            "age_seconds": row["age_seconds"],
                            "had_reverted_tag": reverted,
                            "rev_missing": is_missing,
                            "run_id": run_id,
                        }
                    )

                if is_missing:
                    # A deleted revision cannot be labelled either way. Left
                    # unlabelled on purpose: writing a negative here would put
                    # a non-random slice of the sample into the majority class.
                    continue

                reached_final = max(int(r["checkpoint_seconds"]) for r in pending) >= (
                    FINAL_CHECKPOINT
                )
                if reverted or reached_final:
                    latency = min(int(r["age_seconds"]) for r in pending)
                    label_rows.append(
                        {
                            "revid": revid,
                            "label": reverted,
                            "first_observed_at_utc": checked_at,
                            "detection_latency_seconds": latency,
                            "run_id": run_id,
                        }
                    )

            with connect() as conn, conn.cursor() as cur:
                cur.executemany(INSERT_CHECK_SQL, check_rows)
                if label_rows:
                    cur.executemany(INSERT_LABEL_SQL, label_rows)

            run_ctx.rows_read = len(revids)
            run_ctx.rows_written = len(check_rows)
            run_ctx.partial = missing_count > 0

    result = {
        "due": len(due),
        "revisions": len(revids),
        "checks_written": len(check_rows),
        "labels_written": len(label_rows),
        "reverted": found_positive,
        "missing": missing_count,
    }
    print(
        f"label: {result['revisions']} revisions checked, "
        f"{result['checks_written']} checks, {result['labels_written']} labels, "
        f"{result['reverted']} reverted, {result['missing']} deleted"
    )
    return result


def main() -> int:
    # Registered before any work, so a run that dies mid-read still
    # accounts for what it spent.
    record_on_exit("label")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    run(limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
