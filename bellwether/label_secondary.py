"""Secondary label path (M0-T4, FR-8, FR-11).

Derives outcomes from the *reverting* edit rather than the reverted one.
``mw-undo``, ``mw-rollback`` and ``mw-manual-revert`` are applied at edit time
and arrive in the live feed, so this path sees a revert as soon as ingestion
does — with **no API calls at all**. It reads tags already in the database.

It exists for three reasons, in order of importance:

  1. Two independently derived labels that agree are worth far more than one.
     Their disagreement rate is a published data-quality figure (FR-11), and it
     can only be computed if both are kept.
  2. If the primary path ever breaks — a tag renamed, a job queue backed up —
     this one keeps producing labels, and the gap between them is the alarm.
  3. It is fast and free, so it labels the recent tail long before the primary
     path's checkpoint grid gets there.

**It is deliberately conservative — high precision, known-incomplete recall.**

  * ``mw-undo``: the reverted revision is named in the edit summary ("Undo
    revision 12345 by ..."), and that is used. ``old_revid`` is *not*, because
    undoing an older revision while keeping later ones leaves ``old_revid``
    pointing at an edit that was not reverted at all.
  * ``mw-rollback`` / ``mw-manual-revert``: ``old_revid`` is taken as reverted.
    A rollback can revert a whole run of consecutive edits, so this
    under-counts — it never over-counts.

The recall gap is measurable against the primary path rather than assumed away,
which is the point of running both.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

from bellwether.config import get_settings
from bellwether.db import advisory_lock, connect
from bellwether.runlog import RunContext, new_run_id
from bellwether.usage import record_on_exit

JOB = "label_secondary"
SECONDARY_LOCK_KEY = 815_003

# Undo summaries name the revision they reverted, in one of two shapes:
#
#   Undo revision 1369185025 by [[Special:Contributions/...]]
#   Undid revision [[Special:Diff/1367883184|1367883184]] by [[Special:...]]
#
# The linked form is what English Wikipedia actually emits today: measured
# 2026-08-13, 97 of 97 undo summaries used it and none used the bare number.
# A pattern matching only the bare form silently derived nothing from every
# undo in the sample while reporting a clean run — which is why the count of
# underivable targets is surfaced by the job rather than left implicit.
UNDO_PATTERN = re.compile(
    r"\bUnd(?:o|id) revision (?:\[\[Special:Diff/)?(\d+)",
    re.IGNORECASE,
)

REVERTING_EDITS_SQL = """
SELECT revert_revid AS revid,
       reverted_revid,
       revert_ts    AS event_ts,
       observed_at_utc AS ingested_at_utc,
       method
  FROM outcome.revert_events
 WHERE revert_ts >= now() - make_interval(hours => %(lookback_hours)s)
 ORDER BY revert_ts
"""

INSERT_LABEL_SQL = """
INSERT INTO outcome.labels
    (revid, label, label_source, first_observed_at_utc,
     revert_latency_seconds, detection_latency_seconds, revert_revid, observed_run_id)
SELECT %(reverted_revid)s,
       true,
       'revert_tag',
       %(observed_at)s,
       EXTRACT(epoch FROM %(revert_ts)s::timestamptz - e.event_ts)::bigint,
       EXTRACT(epoch FROM %(observed_at)s::timestamptz - e.event_ts)::bigint,
       %(revert_revid)s,
       %(run_id)s
  FROM landing.rc_events e
 WHERE e.revid = %(reverted_revid)s
   -- A revert cannot precede what it reverts. Guards against a mis-parsed
   -- summary naming an unrelated revision, which would otherwise produce a
   -- negative latency and a silently wrong label.
   AND e.event_ts <= %(revert_ts)s::timestamptz
ON CONFLICT (revid, label_source) DO NOTHING
"""


def reverted_revid_for(edit: dict[str, Any]) -> int | None:
    """Which revision this reverting edit reverted, where it can be known.

    Returns ``None`` when the reverting edit is real but its target cannot be
    derived confidently — an undo whose summary was rewritten or suppressed.
    Those are counted and reported rather than guessed at.
    """
    tags = set(edit.get("tags") or [])

    if "mw-undo" in tags:
        match = UNDO_PATTERN.search(edit.get("comment") or "")
        return int(match.group(1)) if match else None

    if tags & {"mw-rollback", "mw-manual-revert"}:
        return int(edit["old_revid"]) if edit.get("old_revid") else None

    return None


def run(*, lookback_hours: int | None = None) -> dict[str, Any]:
    settings = get_settings()
    run_id = new_run_id()
    lookback = lookback_hours if lookback_hours is not None else settings.secondary_lookback_hours

    with connect() as lock_conn, advisory_lock(lock_conn, SECONDARY_LOCK_KEY) as acquired:
        if not acquired:
            print("label_secondary: another run holds the lock, exiting cleanly")
            return {"skipped": True, "reason": "locked"}

        with RunContext(run_id, job=JOB) as run_ctx, connect() as conn:
            with conn.cursor() as cur:
                cur.execute(REVERTING_EDITS_SQL, {"lookback_hours": lookback})
                reverting = cur.fetchall()

            underivable = 0
            written = 0

            with conn.cursor() as cur:
                for edit in reverting:
                    # Targets are derived once, at ingestion, and stored. The
                    # parse used to happen here, over rc_events — which the
                    # sampling frame had just made blind to 94 per cent of
                    # reverting edits without changing a line of this file.
                    target = edit["reverted_revid"]
                    if target is None:
                        underivable += 1
                        continue
                    cur.execute(
                        INSERT_LABEL_SQL,
                        {
                            "reverted_revid": target,
                            "revert_revid": edit["revid"],
                            "revert_ts": edit["event_ts"],
                            # When *we* first held this information, which is
                            # when the reverting edit was ingested — not when
                            # the revert happened, and not now. Using the
                            # revert's own timestamp would credit the system
                            # with knowing something before it had fetched it,
                            # and detection latency is exactly the quantity
                            # that must not be flattered.
                            "observed_at": edit["ingested_at_utc"],
                            "run_id": run_id,
                        },
                    )
                    written += cur.rowcount

            run_ctx.rows_read = len(reverting)
            run_ctx.rows_written = written

    result = {
        "reverting_edits": len(reverting),
        "labels_written": written,
        "underivable": underivable,
        "api_calls": 0,
    }
    print(
        f"label_secondary: {result['reverting_edits']} reverting edits, "
        f"{result['labels_written']} labels written, "
        f"{result['underivable']} target underivable, 0 API calls"
    )
    return result


def main() -> int:
    # Registered before any work, so a run that dies mid-read still
    # accounts for what it spent.
    record_on_exit("label_secondary")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-hours", type=int, default=None)
    args = parser.parse_args()
    run(lookback_hours=args.lookback_hours)
    return 0


if __name__ == "__main__":
    sys.exit(main())
