"""Retention (M1-FR-8, M1 §6).

Calls `landing.prune_expired`, which is a SECURITY DEFINER function owned by
the database owner. That indirection is the whole design:

  * `bellwether_writer` is denied DELETE by grant, and that denial *is* the
    append-only guarantee.
  * Retention has to delete.
  * Giving the writer DELETE would void the guarantee; putting the owner
    credential into GitHub Actions would void everything.

So the writer may invoke a routine that deletes rows meeting a condition it
does not control and cannot pass in. The age floors live inside the function:
a caller can ask for a *more* conservative cutoff, never a more aggressive one.
Evidence rows are refused outright unless their month has been sealed.

Dry run is the default. A retention job that deletes by default is one bad
argument away from being the worst bug in the system, and unlike every other
bug in this project it would not be recoverable.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from bellwether.config import get_settings
from bellwether.db import advisory_lock, connect
from bellwether.runlog import RunContext, new_run_id

JOB = "retention"
RETENTION_LOCK_KEY = 815_005

PRUNE_SQL = """
SELECT target, rows_affected
  FROM landing.prune_expired(
       p_dry_run       => %(dry_run)s,
       p_raw_days      => %(raw_days)s,
       p_evidence_days => %(evidence_days)s,
       p_cohort_days   => %(cohort_days)s)
"""

SIZE_SQL = """
SELECT pg_size_pretty(sum(pg_total_relation_size(c.oid))) AS pretty,
       sum(pg_total_relation_size(c.oid))                 AS bytes
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname IN ('landing', 'outcome')
   AND c.relkind = 'r'
"""


def database_bytes(conn: Any) -> int:
    row = conn.execute(SIZE_SQL).fetchone()
    return int(row["bytes"] or 0) if row else 0


def run(*, dry_run: bool = True) -> dict[str, Any]:
    settings = get_settings()
    run_id = new_run_id()

    with connect() as lock_conn, advisory_lock(lock_conn, RETENTION_LOCK_KEY) as acquired:
        if not acquired:
            print("retention: another run holds the lock, exiting cleanly")
            return {"skipped": True, "reason": "locked"}

        with RunContext(run_id, job=JOB) as run_ctx, connect() as conn:
            before = database_bytes(conn)
            with conn.cursor() as cur:
                cur.execute(
                    PRUNE_SQL,
                    {
                        "dry_run": dry_run,
                        "raw_days": settings.raw_retention_days,
                        "evidence_days": settings.evidence_retention_days,
                        "cohort_days": settings.cohort_retention_days,
                    },
                )
                results = {row["target"]: int(row["rows_affected"]) for row in cur.fetchall()}
            after = database_bytes(conn)
            run_ctx.rows_written = sum(results.values())
            run_ctx.partial = dry_run

    budget = settings.storage_budget_bytes
    used_pct = round(100.0 * after / budget, 1) if budget else None
    result: dict[str, Any] = {
        "dry_run": dry_run,
        "deleted": results,
        "bytes_before": before,
        "bytes_after": after,
        "budget_bytes": budget,
        "budget_used_pct": used_pct,
    }

    verb = "would delete" if dry_run else "deleted"
    for target, count in results.items():
        print(f"retention: {verb} {count:,} from {target}")
    print(f"retention: {after / 1e6:.1f} MB of a {budget / 1e6:.0f} MB budget ({used_pct}%)")
    if used_pct is not None and used_pct > 80:
        # NFR-4. Loud, because the failure mode after this point is Neon
        # refusing writes, which surfaces as ingestion failing for reasons that
        # look nothing like a storage problem.
        print("retention: WARNING above 80 percent of budget")

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without this it only reports what it would do.",
    )
    args = parser.parse_args()
    run(dry_run=not args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
