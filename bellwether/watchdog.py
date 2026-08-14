"""Fail loudly when the system stops being able to keep its promises (M8 §6).

Every failure this project has found was found by a test or by somebody
looking. Nothing told anyone when the pipeline stopped, and that gap has already
cost something: the API served a three-commit-old build for hours while
`/health` reported `ok`, because the old code was fine.

**The alert is a failing workflow run, not an email.** Email costs money or
depends on a free tier that can be withdrawn (NFR-1). A red mark notifies
whoever watches the repository — one person — and the README says so rather than
implying an on-call system.

**"Has not run yet" is not "has stopped".** This project has spent eight
milestones distinguishing *nobody has looked* from *we looked and found
nothing*. An alert that could not tell them apart would undo that at the last
step, and would cry wolf on every fresh deployment until somebody muted it.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from bellwether.config import get_settings
from bellwether.db import connect
from bellwether.schema import missing

JOB = "watchdog"


class Stalled(RuntimeError):
    """A promise this system makes is no longer being kept."""


# How long a job may be silent before that is a fault rather than a gap.
#
# Generous against the schedule: GitHub's cron is best-effort and a single
# skipped slot is normal (SRS R-5). These are set at roughly four missed runs,
# so an alert means a pattern rather than a hiccup.
SILENCE_MINUTES = {
    "ingest": 60,
    "score": 60,
    "label": 150,
    "label_secondary": 150,
    "metrics": 24 * 60,
    "triggers": 36 * 60,
}

LAST_RUN_SQL = """
SELECT job,
       max(started_at_utc) AS last_run,
       round(EXTRACT(epoch FROM now() - max(started_at_utc)) / 60.0)::int AS minutes_ago
  FROM landing.run_log
 GROUP BY job
"""

REPRODUCTION_SQL = """
SELECT sampled, hash_matched, matched_at_scoring_time, unreproducible
  FROM register.reproductions
 ORDER BY ran_at DESC
 LIMIT 1
"""


def check(conn: Any, *, deployed_build: str | None = None) -> list[str]:
    """Every way this system can be failing quietly. Returns the failures."""
    faults: list[str] = []

    # --- the schema (M8-FR-19) ---------------------------------------------
    absent = missing(conn)
    if absent:
        faults.append(f"schema is behind: {', '.join(absent)}")

    # --- jobs that have stopped (M8-FR-17, M8-FR-21) -----------------------
    seen = {row["job"]: row for row in conn.execute(LAST_RUN_SQL).fetchall()}
    for job, limit in SILENCE_MINUTES.items():
        row = seen.get(job)
        if row is None:
            # Never run. Not a fault — a system deployed an hour ago has not
            # run its daily jobs, and alerting on that would make the watchdog
            # noise from the moment it was switched on.
            continue
        if row["minutes_ago"] > limit:
            faults.append(
                f"{job} last ran {row['minutes_ago']} minutes ago, "
                f"which is past its {limit}-minute limit"
            )

    # --- reproducibility (M8-FR-19) ----------------------------------------
    #
    # M3 promised the scorer's state is reconstructible. A rate below 100% means
    # the register can no longer be independently rebuilt, which is a claim
    # this project makes in public.
    reproduction = conn.execute(REPRODUCTION_SQL).fetchone()
    if reproduction and reproduction["unreproducible"]:
        faults.append(
            f"{reproduction['unreproducible']} of {reproduction['sampled']} sampled "
            f"predictions could not be reproduced"
        )

    # --- the running build (M8-FR-18) --------------------------------------
    #
    # The gap that hid three failed deploys. /health reported ok throughout,
    # because the OLD code was fine and answering.
    if deployed_build:
        expected = get_settings().build_id
        if expected and not expected.startswith(deployed_build[:7]):
            faults.append(
                f"the API is serving build {deployed_build[:7]} but the repository "
                f"is at {expected[:7]} — a deploy has failed silently"
            )

    return faults


def run(*, deployed_build: str | None = None) -> dict[str, Any]:
    with connect() as conn:
        faults = check(conn, deployed_build=deployed_build)

    if not faults:
        print("watchdog: nothing is stalled")
        return {"faults": []}

    print(f"watchdog: {len(faults)} fault(s)")
    for fault in faults:
        print(f"  - {fault}")
    raise Stalled("; ".join(faults))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deployed-build",
        default=None,
        help="the build the API reports serving, for the mismatch check",
    )
    args = parser.parse_args()
    run(deployed_build=args.deployed_build)
    return 0


if __name__ == "__main__":
    sys.exit(main())
