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

**"Still broken" is not "just broke".** The first version failed its run
whenever any fault was present, which is right for a fault that comes and goes
and wrong for one that stays. The reproduction rate fell below 100% on the 14th
and this then failed one hundred consecutive runs — and while it sat red, a
seven-hour ingest outage came and went underneath it unnoticed. A permanently
red alarm does not degrade to a weaker signal; it stops being one, and it takes
every other signal down with it.

So the alert is an edge, not a level. A fault absent last run fails this one. A
fault that has been here for days is printed with its age and re-raised once a
day: loud enough that nothing is quietly tolerated forever, quiet enough that a
new fault beside it is still visible.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, NamedTuple

import psycopg

from bellwether.config import get_settings
from bellwether.db import connect
from bellwether.schema import missing

JOB = "watchdog"


class Stalled(RuntimeError):
    """A promise this system makes is no longer being kept."""


class Fault(NamedTuple):
    """An identity and a wording, deliberately separate.

    The message carries live numbers — "77 minutes ago", "270 of 2557" — and
    changes on almost every run. Keying the alert on the text would make each
    of those a brand-new fault and rebuild the always-red behaviour this
    module exists to end. The key is what stays the same while the fault does.
    """

    key: str
    message: str


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

# How long a standing fault stays quiet before it turns a run red again.
#
# Not zero, which is where this started: a fault that reddens every run buries
# the one that just appeared. Not never, either — a condition nobody is forced
# to look at again is a condition that becomes the furniture. Once a day is the
# smallest cadence that still reaches someone who checks the repository daily,
# which the README says is the entire notification system.
RENOTIFY_HOURS = 24

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

OPEN_FAULTS_SQL = """
SELECT fault_key,
       first_seen,
       round(EXTRACT(epoch FROM now() - first_seen)   / 3600.0)::int AS hours_open,
       round(EXTRACT(epoch FROM now() - last_alerted) / 3600.0)::int AS hours_quiet
  FROM landing.watchdog_faults
"""

RECORD_FAULT_SQL = """
INSERT INTO landing.watchdog_faults (fault_key, message)
VALUES (%(key)s, %(message)s)
ON CONFLICT (fault_key) DO UPDATE
   SET last_seen    = now(),
       message      = EXCLUDED.message,
       -- first_seen is deliberately NOT touched: it is the fault's age, and
       -- the age is what distinguishes a blip from a condition.
       last_alerted = CASE WHEN %(alerted)s THEN now()
                           ELSE landing.watchdog_faults.last_alerted END
"""

CLEAR_FAULT_SQL = "DELETE FROM landing.watchdog_faults WHERE fault_key = ANY(%(keys)s)"


def check(conn: Any, *, deployed_build: str | None = None) -> list[Fault]:
    """Every way this system can be failing quietly. Returns the failures."""
    faults: list[Fault] = []

    # --- the schema (M8-FR-19) ---------------------------------------------
    #
    # One fault, not one per migration: they are applied together by one
    # command, so they are one thing to do something about.
    absent = missing(conn)
    if absent:
        faults.append(Fault("schema", f"schema is behind: {', '.join(absent)}"))

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
                Fault(
                    f"silence:{job}",
                    f"{job} last ran {row['minutes_ago']} minutes ago, "
                    f"which is past its {limit}-minute limit",
                )
            )

    # --- reproducibility (M8-FR-19) ----------------------------------------
    #
    # M3 promised the scorer's state is reconstructible. A rate below 100% means
    # the register can no longer be independently rebuilt, which is a claim
    # this project makes in public.
    reproduction = conn.execute(REPRODUCTION_SQL).fetchone()
    if reproduction and reproduction["unreproducible"]:
        faults.append(
            Fault(
                "reproducibility",
                f"{reproduction['unreproducible']} of {reproduction['sampled']} sampled "
                f"predictions could not be reproduced",
            )
        )

    # --- the running build (M8-FR-18) --------------------------------------
    #
    # The gap that hid three failed deploys. /health reported ok throughout,
    # because the OLD code was fine and answering.
    if deployed_build:
        expected = get_settings().build_id
        if expected and not expected.startswith(deployed_build[:7]):
            faults.append(
                Fault(
                    "deploy",
                    f"the API is serving build {deployed_build[:7]} but the repository "
                    f"is at {expected[:7]} — a deploy has failed silently",
                )
            )

    return faults


def triage(
    conn: Any, faults: list[Fault]
) -> tuple[list[Fault], list[tuple[Fault, int]], list[str]]:
    """Sort the current faults against what has already been reported.

    Returns (new, standing, resolved): the ones that turn this run red, the
    ones already known with how many hours they have been open, and the keys
    that have gone away since last time.

    A standing fault joins `new` once it has been quiet for RENOTIFY_HOURS, so
    "not alerting" never decays into "not mentioned".
    """
    known = {row["fault_key"]: row for row in conn.execute(OPEN_FAULTS_SQL).fetchall()}
    current = {fault.key for fault in faults}

    new: list[Fault] = []
    standing: list[tuple[Fault, int]] = []
    for fault in faults:
        row = known.get(fault.key)
        # Never reported, or reported so long ago that staying quiet would be
        # the same as having forgotten it.
        if row is None or row["hours_quiet"] >= RENOTIFY_HOURS:
            new.append(fault)
        else:
            standing.append((fault, row["hours_open"]))

    resolved = sorted(known.keys() - current)
    return new, standing, resolved


def _record(conn: Any, faults: list[Fault], alerting: set[str], resolved: list[str]) -> None:
    for fault in faults:
        conn.execute(
            RECORD_FAULT_SQL,
            {"key": fault.key, "message": fault.message, "alerted": fault.key in alerting},
        )
    if resolved:
        # Deleted rather than kept with a cleared_at. A fault that returns
        # should read as new, because that is what it is to whoever has to act
        # on it, and the history of what actually happened is in run_log.
        conn.execute(CLEAR_FAULT_SQL, {"keys": resolved})


def run(*, deployed_build: str | None = None) -> dict[str, Any]:
    with connect() as conn:
        faults = check(conn, deployed_build=deployed_build)

        try:
            new, standing, resolved = triage(conn, faults)
            remembered = True
        except psycopg.errors.UndefinedTable:
            # Migration 030 has not been applied to this database yet.
            #
            # Falling back to the old always-red behaviour rather than to
            # silence: without the table there is no way to tell a new fault
            # from a standing one, and of the two ways to be wrong, crying wolf
            # is the recoverable one.
            conn.rollback()
            new, standing, resolved, remembered = faults, [], [], False

        if remembered:
            _record(conn, faults, {fault.key for fault in new}, resolved)

    headline = (
        f"watchdog: {len(new)} new, {len(standing)} standing, {len(resolved)} cleared"
        if faults or resolved
        else "watchdog: nothing is stalled"
    )
    print(headline)

    for fault in new:
        print(f"  - {fault.message}")
    for fault, hours in standing:
        # Printed on a green run too. Not alerting must never decay into not
        # mentioning — the standing fault is the one most likely to be forgotten.
        print(f"  standing ({hours}h)  {fault.message}")
    for key in resolved:
        print(f"  cleared         {key}")

    if not remembered:
        print(
            "  landing.watchdog_faults is missing, so every fault reads as new. "
            "Apply migration 030: python scripts/bootstrap_database.py <owner-url>"
        )

    if not new:
        return {
            "faults": [],
            "standing": [fault.message for fault, _ in standing],
            "resolved": resolved,
        }

    raise Stalled("; ".join(fault.message for fault in new))


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
