"""Gap detection and healing (M1-FR-12 to FR-14).

M0 proved that an ordinary run heals an outage: the cursor did not advance
past the hole, so the next run simply continued from where it stopped. This
job exists for the cases that cannot fix themselves:

  * a gap **behind** the cursor — a page that failed after its neighbours
    committed, or a window whose events arrived out of order;
  * a gap longer than one run's page budget, which an ordinary run would only
    partially close before hitting its cap;
  * a gap that predates a change of frame or a restore.

**Why a ten-minute threshold still works under sampling.** At the M1 frame the
pipeline keeps roughly 9,300 events a day — about 6.4 a minute, so a mean
spacing near nine seconds. Even in the quietest measured hour the rate was
5/minute. Under arrivals that dense, a ten-minute silence is not thinning: it
is absence. The threshold survives the frame change that cut ingestion volume
by an order of magnitude, and it was worth checking rather than assuming.

**Gaps are derived, never stored.** A stored gap goes stale as soon as part of
it fills — its recorded boundaries would describe a hole that no longer has
those edges. Only the *attempts* are recorded, which is what makes a permanent
gap distinguishable from an unlucky one.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from bellwether import frame
from bellwether.config import get_settings
from bellwether.db import advisory_lock, connect
from bellwether.http import MediaWikiClient
from bellwether.ingest import insert_page
from bellwether.mediawiki import iter_recent_changes
from bellwether.runlog import RunContext, new_run_id, utcnow

JOB = "gapfill"
GAPFILL_LOCK_KEY = 815_004

GAPS_SQL = """
WITH ordered AS (
    SELECT event_ts,
           lag(event_ts) OVER (ORDER BY event_ts) AS prev
      FROM landing.rc_events
     WHERE event_ts >= now() - make_interval(days => %(retention_days)s)
)
SELECT prev       AS gap_from,
       event_ts   AS gap_to,
       EXTRACT(epoch FROM event_ts - prev)::bigint AS seconds
  FROM ordered
 WHERE prev IS NOT NULL
   AND event_ts - prev > make_interval(secs => %(threshold_seconds)s)
 ORDER BY prev
"""

ATTEMPTS_SQL = """
SELECT count(*)                       AS attempts,
       COALESCE(sum(rows_added), 0)   AS rows_added,
       max(attempted_at)              AS last_attempt
  FROM landing.gap_attempts
 WHERE gap_from_utc <= %(gap_from)s
   AND gap_to_utc   >= %(gap_to)s
"""

LOG_ATTEMPT_SQL = """
INSERT INTO landing.gap_attempts
    (gap_from_utc, gap_to_utc, rows_added, api_calls, run_id)
VALUES (%(gap_from)s, %(gap_to)s, %(rows_added)s, %(api_calls)s, %(run_id)s)
"""


@dataclass(frozen=True)
class Gap:
    gap_from: datetime
    gap_to: datetime
    seconds: int
    attempts: int
    rows_recovered: int

    @property
    def minutes(self) -> float:
        return self.seconds / 60.0


def find_gaps(conn: Any, *, retention_days: int, threshold_seconds: int) -> list[Gap]:
    """Every gap currently visible in the retained window, oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            GAPS_SQL,
            {"retention_days": retention_days, "threshold_seconds": threshold_seconds},
        )
        rows = cur.fetchall()

    gaps: list[Gap] = []
    for row in rows:
        with conn.cursor() as cur:
            cur.execute(ATTEMPTS_SQL, {"gap_from": row["gap_from"], "gap_to": row["gap_to"]})
            history = cur.fetchone() or {}
        gaps.append(
            Gap(
                gap_from=row["gap_from"],
                gap_to=row["gap_to"],
                seconds=int(row["seconds"]),
                attempts=int(history.get("attempts") or 0),
                rows_recovered=int(history.get("rows_added") or 0),
            )
        )
    return gaps


def is_permanent(gap: Gap, *, max_attempts: int, horizon_days: int) -> bool:
    """Whether to stop trying (M1-FR-14).

    Two ways a gap becomes permanent, and neither is a failure to hide:

      * It predates the wiki's own `recentchanges` horizon. The data is gone
        from the source; no number of retries will conjure it back.
      * It has been attempted repeatedly and yielded nothing. Either the
        window genuinely had no edits the frame would keep, or the source
        cannot serve it. Retrying forever would burn a free API budget to
        learn the same thing on a loop.

    A permanent gap is excluded from coverage claims rather than silently
    counted as covered.
    """
    if gap.gap_to < utcnow() - timedelta(days=horizon_days):
        return True
    return gap.attempts >= max_attempts and gap.rows_recovered == 0


def heal(conn: Any, client: MediaWikiClient, gap: Gap, run_id: Any) -> int:
    """Re-ingest one gap. Returns rows added.

    Deliberately does NOT touch the cursor. The cursor tracks the live front of
    ingestion; a gap sits behind it by definition, and letting a heal move it
    would rewind live ingestion to close a hole — trading a small gap for a
    large one.
    """
    added = 0
    tag_cache: dict[str, int] = {}
    for page in iter_recent_changes(client, start=gap.gap_from, max_pages=8):
        fresh = [e for e in page if e["event_ts"] <= gap.gap_to and frame.in_sample(e)]
        if fresh:
            added += insert_page(conn, fresh, run_id, tag_cache)
        if max(e["event_ts"] for e in page) > gap.gap_to:
            break
    return added


def run(*, max_gaps: int = 5) -> dict[str, Any]:
    settings = get_settings()
    run_id = new_run_id()

    with connect() as lock_conn, advisory_lock(lock_conn, GAPFILL_LOCK_KEY) as acquired:
        if not acquired:
            print("gapfill: another run holds the lock, exiting cleanly")
            return {"skipped": True, "reason": "locked"}

        with connect() as conn:
            gaps = find_gaps(
                conn,
                retention_days=settings.raw_retention_days,
                threshold_seconds=settings.gap_threshold_seconds,
            )

        permanent = [
            g
            for g in gaps
            if is_permanent(
                g,
                max_attempts=settings.gap_max_attempts,
                horizon_days=settings.recentchanges_horizon_days,
            )
        ]
        healable = [g for g in gaps if g not in permanent][:max_gaps]

        healed = 0
        recovered = 0
        with RunContext(run_id, job=JOB) as run_ctx:
            if healable:
                with MediaWikiClient() as client, connect() as conn:
                    for gap in healable:
                        added = heal(conn, client, gap, run_id)
                        with conn.cursor() as cur:
                            cur.execute(
                                LOG_ATTEMPT_SQL,
                                {
                                    "gap_from": gap.gap_from,
                                    "gap_to": gap.gap_to,
                                    "rows_added": added,
                                    "api_calls": client.calls,
                                    "run_id": run_id,
                                },
                            )
                        recovered += added
                        if added:
                            healed += 1
                    run_ctx.api_calls = client.calls
            run_ctx.rows_written = recovered
            run_ctx.partial = bool(permanent)

    result = {
        "gaps": len(gaps),
        "permanent": len(permanent),
        "attempted": len(healable),
        "healed": healed,
        "rows_recovered": recovered,
    }
    print(
        f"gapfill: {result['gaps']} gaps ({result['permanent']} permanent), "
        f"attempted {result['attempted']}, healed {result['healed']}, "
        f"{result['rows_recovered']} rows recovered"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-gaps", type=int, default=5)
    parser.add_argument(
        "--list", action="store_true", help="Report gaps without attempting to heal them"
    )
    args = parser.parse_args()

    if args.list:
        settings = get_settings()
        with connect() as conn:
            gaps = find_gaps(
                conn,
                retention_days=settings.raw_retention_days,
                threshold_seconds=settings.gap_threshold_seconds,
            )
        for gap in gaps:
            mark = (
                "PERMANENT"
                if is_permanent(
                    gap,
                    max_attempts=settings.gap_max_attempts,
                    horizon_days=settings.recentchanges_horizon_days,
                )
                else "open"
            )
            print(
                f"  {mark:<10}{gap.minutes:>10.1f} min  "
                f"{gap.gap_from:%Y-%m-%d %H:%M}  ->  {gap.gap_to:%Y-%m-%d %H:%M}  "
                f"(attempts {gap.attempts}, recovered {gap.rows_recovered})"
            )
        if not gaps:
            print("  no gaps")
        return 0

    run(max_gaps=args.max_gaps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
