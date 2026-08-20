"""Assert that replaying reproduces the persisted state (M3-FR-12, M3-FR-13).

The replay in `state.py` is the definition of what the state should be. The
persisted rows are what the online path actually produced, one ten-minute batch
at a time. This job replays the window, compares, and **fails** on any
difference.

**It does not repair by default, and that is the point.** M3-FR-13: a job that
quietly corrects drift removes the only signal that something is producing it.
The overwrite is available behind `--repair`, as a deliberate act taken after
someone has looked at what diverged and why.

**Scoping, so the check means something.** A replay over N days cannot
reproduce a counter that includes events older than N days, and raw events are
pruned at thirty. So the comparison is restricted to editors and pages whose
persisted `first_seen_utc` falls inside the window — those, and only those, had
their entire history inside what the replay can see. Everything older is
counted and reported as out of scope rather than quietly folded into an
agreement rate it does not belong in.

The window is deliberately shorter than the retention horizon, so nothing
inside it has been pruned out from under the replay.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from bellwether import state
from bellwether.db import advisory_lock, connect
from bellwether.runlog import RunContext, new_run_id
from bellwether.usage import record_on_exit

JOB = "reconcile"
RECONCILE_LOCK_KEY = 815_008

# Shorter than raw_retention_days (30) by a wide margin, so the replay is never
# comparing against counters built from events that have since been pruned.
WINDOW_DAYS = 7

# The fields that feed features. Anything here being wrong is a wrong
# prediction, which is why they are compared rather than sampled.
EDITOR_FIELDS = ("edits", "reverts_performed", "edits_reverted", "first")
PAGE_FIELDS = ("edits", "reverted", "first")


class StateDivergence(RuntimeError):
    """Replay and persisted state disagree. Deliberately fatal (M3-FR-13)."""


@dataclass(frozen=True)
class Divergence:
    kind: str
    key: str
    field: str
    replayed: Any
    persisted: Any

    def __str__(self) -> str:
        return (
            f"{self.kind} {self.key!r}.{self.field}: "
            f"replay says {self.replayed!r}, stored says {self.persisted!r}"
        )


IN_WINDOW_EDITORS_SQL = """
SELECT user_key, first_seen_utc, edits_seen, reverts_performed, edits_reverted
  FROM landing.editor_state
 WHERE first_seen_utc >= %(window_start)s
"""

IN_WINDOW_PAGES_SQL = """
SELECT page_key, first_seen_utc, edits_seen, edits_reverted
  FROM landing.page_state
 WHERE first_seen_utc >= %(window_start)s
"""


def _editor_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "edits": row["edits_seen"],
        "reverts_performed": row["reverts_performed"],
        "edits_reverted": row["edits_reverted"],
        "first": row["first_seen_utc"],
    }


def _page_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "edits": row["edits_seen"],
        "reverted": row["edits_reverted"],
        "first": row["first_seen_utc"],
    }


def in_scope(stored: dict[str, dict[str, Any]], *, window_start: datetime) -> set[str]:
    """The keys a replay over this window can speak for at all.

    A key whose stored `first_seen_utc` predates the window has history the
    replay cannot see, so the replayed counter is a strict undercount of it.
    Both the comparison and the repair read scope from here, because the two
    disagreeing is the failure mode that matters: judging a key the repair
    would not fix is merely noisy, while REPAIRING a key the comparison never
    judged writes a seven-day count over a lifetime one, silently and for
    exactly the editors with the longest histories.
    """
    return {key for key, row in stored.items() if row["first"] >= window_start}


def scoped(replayed: dict[str, Any], *, editors: set[str], pages: set[str]) -> dict[str, Any]:
    """The replayed state cut down to the keys a repair may write.

    The frontier rides along: it is persisted through GREATEST, so it can only
    ever move forwards and there is nothing for a window to truncate.
    """
    return {
        "editors": {k: v for k, v in replayed.get("editors", {}).items() if k in editors},
        "pages": {k: v for k, v in replayed.get("pages", {}).items() if k in pages},
        "max_user_id": replayed.get("max_user_id", 0),
    }


def compare(
    replayed: dict[str, Any],
    stored_editors: dict[str, dict[str, Any]],
    stored_pages: dict[str, dict[str, Any]],
    *,
    window_start: datetime,
) -> tuple[list[Divergence], dict[str, int]]:
    """Compare replayed state against what the online path persisted.

    Only keys whose stored `first_seen_utc` is inside the window are judged.
    A key the replay has never heard of is not a divergence — it is simply
    older than the window, and saying otherwise would make the agreement rate
    a function of how far back the job happens to look.
    """
    found: list[Divergence] = []
    counts = {"editors_checked": 0, "pages_checked": 0, "missing": 0, "unexpected": 0}

    for kind, stored, replay_bucket, fields, builder in (
        ("editor", stored_editors, replayed.get("editors", {}), EDITOR_FIELDS, "editors_checked"),
        ("page", stored_pages, replayed.get("pages", {}), PAGE_FIELDS, "pages_checked"),
    ):
        scope = in_scope(stored, window_start=window_start)
        for key, stored_row in stored.items():
            if key not in scope:
                continue
            counts[builder] += 1
            replay_row = replay_bucket.get(key)
            if replay_row is None:
                # Stored state claims an editor the replay never saw. The events
                # that produced it are gone, or were never there.
                counts["missing"] += 1
                found.append(Divergence(kind, key, "<exists>", None, stored_row["edits"]))
                continue
            for field in fields:
                if replay_row[field] != stored_row[field]:
                    found.append(Divergence(kind, key, field, replay_row[field], stored_row[field]))

    return found, counts


FRONTIER_SQL = "SELECT value_bigint FROM landing.pipeline_state WHERE state_key = 'max_user_id'"


def run(*, days: int = WINDOW_DAYS, repair: bool = False, show: int = 15) -> dict[str, Any]:
    run_id = new_run_id()
    window_start = datetime.now(UTC) - timedelta(days=days)

    with connect() as lock_conn, advisory_lock(lock_conn, RECONCILE_LOCK_KEY) as acquired:
        if not acquired:
            print("reconcile: another run holds the lock, exiting cleanly")
            return {"skipped": True}

        with RunContext(run_id, job=JOB, window_from=window_start) as ctx, connect() as conn:
            result = state.replay(conn, days=days)

            with conn.cursor() as cur:
                cur.execute(IN_WINDOW_EDITORS_SQL, {"window_start": window_start})
                stored_editors = {r["user_key"]: _editor_row(r) for r in cur.fetchall()}
                cur.execute(IN_WINDOW_PAGES_SQL, {"window_start": window_start})
                stored_pages = {r["page_key"]: _page_row(r) for r in cur.fetchall()}
                cur.execute(FRONTIER_SQL)
                row = cur.fetchone()
                stored_frontier = int(row["value_bigint"]) if row and row["value_bigint"] else 0

            found, counts = compare(
                result["state"], stored_editors, stored_pages, window_start=window_start
            )

            # The frontier is global and monotone, so a windowed replay seeing a
            # LOWER maximum is expected — the account may predate the window.
            # Seeing a higher one is not: the online path missed an account it
            # had in front of it.
            replayed_frontier = int(result["state"].get("max_user_id", 0))
            if replayed_frontier > stored_frontier:
                found.append(
                    Divergence(
                        "frontier", "max_user_id", "value", replayed_frontier, stored_frontier
                    )
                )

            ctx.rows_read = result["events"]
            ctx.partial = bool(found)

            if repair and found:
                # The same scope the comparison used, and for a stronger
                # reason. `result["state"]` holds every key with an event in
                # the window, including editors whose stored counters were
                # built over months — the ones the comparison deliberately
                # refuses to judge because a windowed replay undercounts them.
                # Writing those replayed counters would overwrite a lifetime
                # count with a seven-day one, for exactly the editors carrying
                # the most history, and nothing would report it: the next run
                # leaves them out of scope again and calls the table clean.
                repairable = scoped(
                    result["state"],
                    editors=in_scope(stored_editors, window_start=window_start),
                    pages=in_scope(stored_pages, window_start=window_start),
                )
                editors, pages = state.persist(conn, repairable, counters_only=True)
                # Same reason as bellwether.state's rebuild: a repair writes the
                # replayed counters, so the ledger has to record what they now
                # include, or the scorer folds those events again on top of it.
                #
                # Every replayed revid, not just the ones whose keys were
                # rewritten. The ledger is per-event while a repair is per-key,
                # and an event that moves an out-of-scope key too cannot be
                # recorded for one side and not the other. Recording it costs
                # the unrepaired side at most the single edit it had not been
                # scored for yet; leaving it out lets that event be folded a
                # second time into a counter that already contains it, which is
                # the doubling this ledger exists to prevent.
                state.record_folded(conn, result["revids"], source="replay")
                ctx.rows_written = editors + pages

    checked = counts["editors_checked"] + counts["pages_checked"]
    agreement = 1.0 - (len(found) / checked) if checked else 1.0

    print(f"reconcile: replayed {result['events']:,} events over {days}d")
    print(
        f"  in-window keys checked: {counts['editors_checked']:,} editors, "
        f"{counts['pages_checked']:,} pages"
    )
    print(f"  divergences: {len(found):,}   agreement {agreement:.4%}")

    if found:
        by_field: dict[str, int] = {}
        for d in found:
            by_field[f"{d.kind}.{d.field}"] = by_field.get(f"{d.kind}.{d.field}", 0) + 1
        print("  by field:")
        for field, n in sorted(by_field.items(), key=lambda kv: -kv[1]):
            print(f"    {field:<32} {n:>7,}")
        print(f"  first {min(show, len(found))}:")
        for d in found[:show]:
            print(f"    {d}")

    summary = {
        "events": result["events"],
        "checked": checked,
        "divergences": len(found),
        "agreement": round(agreement, 6),
        "repaired": repair and bool(found),
    }

    if found and not repair:
        # M3-FR-13. Loud, not silent, and not self-healing.
        raise StateDivergence(
            f"{len(found):,} divergences between replayed and persisted state "
            f"across {checked:,} in-window keys (agreement {agreement:.4%}). "
            f"Not repairing: a job that quietly corrects drift removes the only "
            f"signal that something is producing it. Re-run with --repair once "
            f"the cause is understood."
        )

    return summary


def main() -> int:
    # Registered before any work, so a run that dies mid-read still
    # accounts for what it spent.
    record_on_exit("reconcile")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=WINDOW_DAYS)
    parser.add_argument(
        "--repair",
        action="store_true",
        help="overwrite persisted state with the replay. Deliberate act, never scheduled.",
    )
    parser.add_argument("--show", type=int, default=15)
    args = parser.parse_args()
    run(days=args.days, repair=args.repair, show=args.show)
    return 0


if __name__ == "__main__":
    sys.exit(main())
