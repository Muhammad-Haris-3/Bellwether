"""Point-in-time editor and page state (SRS FR-12, M2-FR-8 to FR-13).

**One function, two callers.** :func:`observe` folds an event into the running
state, and :func:`history_for` reads the state as it stood *before* an event.
Batch replay and online scoring both go through them, so there is no second
implementation to drift — train/serve skew is a bug you write once and discover
after deployment.

**Correctness is the order of operations.** For each event, in ascending
`event_ts`: read the state, emit features, *then* fold the event in. Emitting
after folding would put the event into its own history, which is the most
ordinary way to build a model that cannot be reproduced in production. The
replay loop below does it in that order and the tests check that it does.

**This may not be worth having, and M2 measures rather than assumes.** The
frame keeps 3% of registered edits, so most registered editors will appear with
no prior history at all. Coverage is measured per stratum (M2-FR-12) and
history features are reported as uninformative for a stratum below 10% rather
than included and left to look like signal.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from typing import Any

from bellwether.db import advisory_lock, connect
from bellwether.runlog import RunContext, new_run_id
from bellwether.usage import record_on_exit

JOB = "state"
STATE_LOCK_KEY = 815_006

EMPTY: dict[str, Any] = {
    "editor_edits_seen": 0,
    "editor_reverts_performed": 0,
    "editor_edits_reverted": 0,
    "editor_first_seen": None,
    "page_edits_seen": 0,
    "page_edits_reverted": 0,
    "max_user_id_seen": 0,
}


def user_key(event: dict[str, Any]) -> str:
    """Keyed on the name, which exists for every editor on every wiki.

    Temporary accounts carry real user ids, but an IP edit on a wiki without
    them has none, and one key has to work for both.
    """
    return str(event.get("user_name") or f"#{event.get('user_id') or 0}")


def page_key(event: dict[str, Any]) -> str:
    return str(event.get("title") or "")


def observe(state: dict[str, Any], event: dict[str, Any], *, was_reverted: bool = False) -> None:
    """Fold one event into the running state. Mutates in place.

    Called AFTER features for this event have been emitted. `was_reverted` is
    supplied only when the outcome is already known to have occurred before the
    next event being scored — never from the event's own future.
    """
    key_u, key_p = user_key(event), page_key(event)
    # NOTE: editor["first"] is deliberately NOT made order-independent, unlike
    # its page counterpart and unlike `last` below.
    #
    # It is set once, here, and never lowered — so out-of-order arrival leaves
    # it at the first event PROCESSED rather than the earliest that happened,
    # which reads an account as newer than it is. That is a real inaccuracy and
    # it is left in place on purpose: `first` is the source of
    # editor_first_seen, hence account_newness, the feature carrying most of the
    # model's margin (0.107 with it, 0.039 without). Taking the min would change
    # what the model sees, so it is a scoring decision under PREREGISTRATION.md
    # rather than a bug fix, and it belongs to a retrain and a recorded
    # decision — not to a commit repairing a crash.
    #
    # The persist path is already asymmetric in exactly this way: it upserts
    # `first` through LEAST, so the database does lower it across runs even
    # though a single in-memory pass does not.
    editor = state.setdefault("editors", {}).setdefault(
        key_u,
        {
            "first": event["event_ts"],
            "last": event["event_ts"],
            "edits": 0,
            "reverts_performed": 0,
            "edits_reverted": 0,
        },
    )
    editor["edits"] += 1
    # max, not assignment.
    #
    # Events do not arrive in timestamp order. Ingestion moves forward from a
    # cursor while gapfill reaches back behind it, so a batch can fold an edit
    # from 10:22 after one from 10:52. Assigning made `last` whichever event was
    # processed most recently rather than the latest one that happened, and for
    # a user_key first seen on the later edit that put `last` BEFORE `first` —
    # which landing.editor_state rejects by CHECK constraint, and which stopped
    # scoring outright:
    #
    #   CheckViolation: new row for relation "editor_state" violates check
    #   constraint "editor_state_seen_ordered"
    #   DETAIL: Failing row contains (Starklinson, 10:52:55, 10:22:04, 7, 0, 0)
    #
    # The persist path already had this right — it upserts through GREATEST, so
    # a row that reached the database once could never be walked backwards. Only
    # the first INSERT for a key was exposed, because there was nothing to take
    # the GREATEST against. This makes the in-memory fold agree with the SQL
    # that stores it.
    #
    # `last` is persisted but is not a feature: history_for reads edits,
    # reverts_performed, edits_reverted and first. So this cannot move a score.
    editor["last"] = max(editor["last"], event["event_ts"])
    if event.get("is_reverting"):
        editor["reverts_performed"] += 1
    if was_reverted:
        editor["edits_reverted"] += 1

    page = state.setdefault("pages", {}).setdefault(
        key_p, {"first": event["event_ts"], "last": event["event_ts"], "edits": 0, "reverted": 0}
    )
    page["edits"] += 1
    # Same defect, same fix, and page_state carries the same CHECK. It has not
    # failed yet only because it needs a page whose first two folded edits
    # arrive reversed; nothing prevents it. Neither `first` nor `last` on a page
    # is a feature, so both are made order-independent here.
    page["first"] = min(page["first"], event["event_ts"])
    page["last"] = max(page["last"], event["event_ts"])
    if was_reverted:
        page["reverted"] += 1

    # The account-id frontier, folded in like everything else.
    #
    # Reading max(user_id) over the whole table instead would let an edit see
    # accounts created after it — the leak the knowability guard exists to
    # prevent, reintroduced through an aggregate rather than a column.
    user_id = int(event.get("user_id") or 0)
    if user_id > state.get("max_user_id", 0):
        state["max_user_id"] = user_id


def history_for(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """The history view for an event: everything folded in *before* it."""
    editor = state.get("editors", {}).get(user_key(event))
    page = state.get("pages", {}).get(page_key(event))
    return {
        "editor_edits_seen": editor["edits"] if editor else 0,
        "editor_reverts_performed": editor["reverts_performed"] if editor else 0,
        "editor_edits_reverted": editor["edits_reverted"] if editor else 0,
        "editor_first_seen": editor["first"] if editor else None,
        "page_edits_seen": page["edits"] if page else 0,
        "page_edits_reverted": page["reverted"] if page else 0,
        "max_user_id_seen": state.get("max_user_id", 0),
        "event_ts": event["event_ts"],
    }


# When this system first KNEW an edit had been reverted.
#
# Not when the revert happened. Those are different moments and the gap is not
# small: MediaWiki applies the mw-reverted tag from a deferred job, and the
# label pass that finds it runs every thirty minutes. Folding a revert in at
# revert_ts builds training histories out of knowledge production could not
# have had, which is a leak — quieter than a leaked column, and it survives the
# knowability guard untouched, because no feature depends on the future of the
# event it describes. The STATE does.
#
# Both discovery paths are considered and the earliest wins, because either one
# finding it is this system knowing it.
KNOWN_AT_SQL = """
        (SELECT min(t) FROM (
             SELECT r.observed_at_utc AS t
               FROM outcome.revert_events r WHERE r.reverted_revid = e.revid
             UNION ALL
             SELECT l.first_observed_at_utc
               FROM outcome.labels l WHERE l.revid = e.revid AND l.label
         ) AS discovery)
"""

REPLAY_SQL = f"""
SELECT e.revid, e.event_ts, e.title, e.user_name, e.user_id, e.sampling_stratum,
       (e.tags && ARRAY['mw-undo','mw-rollback','mw-manual-revert']) AS is_reverting,
       {KNOWN_AT_SQL} AS reverted_known_at
  FROM landing.rc_events e
 WHERE e.event_ts >= now() - make_interval(days => %(days)s)
 ORDER BY e.event_ts, e.revid
"""


def replay(conn: Any, *, days: int) -> dict[str, Any]:
    """Rebuild state by folding every event in time order.

    Reverts are applied at the moment this system LEARNED of them — not at the
    moment their target was edited, and not at the moment the revert happened.
    Folding at the edit would make every later history read aware of something
    that had not occurred; folding at the revert would make it aware of
    something nobody had told us yet, which is the subtler of the two and the
    one that survives review.

    Reverts still pending at the end of the loop are applied if they are known
    by now. The state this leaves behind is what the next event to arrive would
    read, which is the only definition under which the online path can agree
    with it.
    """
    with conn.cursor() as cur:
        cur.execute(REPLAY_SQL, {"days": days})
        events = cur.fetchall()

    state: dict[str, Any] = {}
    pending: list[tuple[datetime, dict[str, Any]]] = []
    coverage: dict[str, Any] = {"scored": 0, "with_history": 0, "by_stratum": {}}

    for event in events:
        now = event["event_ts"]

        # Apply any revert that had already happened by this point.
        still_pending = []
        for revert_ts, reverted in pending:
            if revert_ts <= now:
                observe_revert(state, reverted)
            else:
                still_pending.append((revert_ts, reverted))
        pending = still_pending

        history = history_for(state, event)
        stratum = event["sampling_stratum"]
        bucket = coverage["by_stratum"].setdefault(stratum, {"scored": 0, "with_history": 0})
        bucket["scored"] += 1
        coverage["scored"] += 1
        if history["editor_edits_seen"] > 0:
            bucket["with_history"] += 1
            coverage["with_history"] += 1

        observe(state, event)
        if event["reverted_known_at"] is not None:
            pending.append((event["reverted_known_at"], event))

    # Everything known by now, applied. Without this the replay's final state is
    # "as of the last event in the window" while the online path's is "as of
    # now", and the two could never agree on the reverts discovered in between.
    horizon = datetime.now(UTC)
    for known_at, reverted in pending:
        if known_at <= horizon:
            observe_revert(state, reverted)

    return {"state": state, "coverage": coverage, "events": len(events)}


def observe_revert(state: dict[str, Any], event: dict[str, Any]) -> None:
    """Record that a previously seen edit turned out to be reverted."""
    editor = state.get("editors", {}).get(user_key(event))
    if editor:
        editor["edits_reverted"] += 1
    page = state.get("pages", {}).get(page_key(event))
    if page:
        page["reverted"] += 1


PENDING_REVERTS_SQL = f"""
SELECT e.revid, e.user_name, e.user_id, e.title, k.known_at
  FROM landing.rc_events e
  JOIN LATERAL (SELECT {KNOWN_AT_SQL} AS known_at) k ON true
 WHERE k.known_at IS NOT NULL
   AND k.known_at <= now()
   AND e.event_ts >= now() - make_interval(days => %(days)s)
   AND NOT EXISTS (SELECT 1 FROM landing.state_applied_reverts a WHERE a.revid = e.revid)
 ORDER BY k.known_at, e.revid
"""

APPLY_EDITOR_REVERT_SQL = """
UPDATE landing.editor_state SET edits_reverted = edits_reverted + 1 WHERE user_key = %(key)s
"""

APPLY_PAGE_REVERT_SQL = """
UPDATE landing.page_state SET edits_reverted = edits_reverted + 1 WHERE page_key = %(key)s
"""

MARK_APPLIED_SQL = """
INSERT INTO landing.state_applied_reverts (revid, counters_moved)
VALUES (%(revid)s, %(moved)s)
ON CONFLICT (revid) DO NOTHING
"""


def apply_reverts(conn: Any, *, days: int = 30) -> dict[str, int]:
    """Fold newly discovered reverts into the persisted counters.

    The half of the outcome the online path could not do for itself. Scoring
    folds an event in the moment it is scored, but at that moment nobody knows
    whether it will be reverted — that is the premise of the whole project — and
    nothing came back later to say so. `editor_edits_reverted` and
    `page_edits_reverted` therefore reached the model as zero in production
    while training saw real values.

    Exactly once per reverted edit, enforced by a primary key rather than by a
    timestamp watermark. Applied in discovery order, which is the order the
    replay uses, so the two arrive at the same counters.
    """
    applied = moved = missing = 0
    with conn.cursor() as cur:
        cur.execute(PENDING_REVERTS_SQL, {"days": days})
        pending = cur.fetchall()

        for event in pending:
            cur.execute(APPLY_EDITOR_REVERT_SQL, {"key": user_key(event)})
            touched = cur.rowcount
            cur.execute(APPLY_PAGE_REVERT_SQL, {"key": page_key(event)})
            touched += cur.rowcount
            # An edit whose editor was never seen online cannot be incremented.
            # It is still recorded as handled, or every run would retry it
            # forever and the count would never mean anything.
            if touched:
                moved += 1
            else:
                missing += 1
            cur.execute(MARK_APPLIED_SQL, {"revid": event["revid"], "moved": bool(touched)})
            applied += 1

    return {"applied": applied, "counters_moved": moved, "no_row_to_move": missing}


LOAD_EDITORS_SQL = """
SELECT user_key, first_seen_utc, last_seen_utc, edits_seen,
       reverts_performed, edits_reverted
  FROM landing.editor_state
 WHERE user_key = ANY(%(keys)s)
"""

LOAD_PAGES_SQL = """
SELECT page_key, first_seen_utc, last_seen_utc, edits_seen, edits_reverted
  FROM landing.page_state
 WHERE page_key = ANY(%(keys)s)
"""

LOAD_FRONTIER_SQL = """
SELECT value_bigint FROM landing.pipeline_state WHERE state_key = 'max_user_id'
"""


def load_for(conn: Any, events: list[dict[str, Any]]) -> dict[str, Any]:
    """State for exactly the editors and pages in this batch.

    Not the whole table. At steady state these hold tens of thousands of rows,
    and reading all of them every ten minutes to score a few dozen events would
    be absurd — and would get worse every day the project runs.

    The id frontier is global and always read, because it is one number.
    """
    users = sorted({user_key(e) for e in events})
    pages = sorted({page_key(e) for e in events})
    st: dict[str, Any] = {"editors": {}, "pages": {}}

    with conn.cursor() as cur:
        cur.execute(LOAD_EDITORS_SQL, {"keys": users})
        for row in cur.fetchall():
            st["editors"][row["user_key"]] = {
                "first": row["first_seen_utc"],
                "last": row["last_seen_utc"],
                "edits": row["edits_seen"],
                "reverts_performed": row["reverts_performed"],
                "edits_reverted": row["edits_reverted"],
            }
        cur.execute(LOAD_PAGES_SQL, {"keys": pages})
        for row in cur.fetchall():
            st["pages"][row["page_key"]] = {
                "first": row["first_seen_utc"],
                "last": row["last_seen_utc"],
                "edits": row["edits_seen"],
                "reverted": row["edits_reverted"],
            }
        cur.execute(LOAD_FRONTIER_SQL)
        row = cur.fetchone()
        st["max_user_id"] = int(row["value_bigint"]) if row and row["value_bigint"] else 0

    return st


PERSIST_FRONTIER_SQL = """
INSERT INTO landing.pipeline_state (state_key, value_bigint, updated_at_utc)
VALUES ('max_user_id', %(value)s, now())
ON CONFLICT (state_key) DO UPDATE
   SET value_bigint = GREATEST(landing.pipeline_state.value_bigint, EXCLUDED.value_bigint),
       updated_at_utc = EXCLUDED.updated_at_utc
"""

PERSIST_EDITOR_SQL = """
INSERT INTO landing.editor_state
    (user_key, first_seen_utc, last_seen_utc, edits_seen, reverts_performed, edits_reverted)
VALUES (%(key)s, %(first)s, %(last)s, %(edits)s, %(reverts)s, %(reverted)s)
ON CONFLICT (user_key) DO UPDATE SET
    first_seen_utc    = LEAST(landing.editor_state.first_seen_utc, EXCLUDED.first_seen_utc),
    last_seen_utc     = GREATEST(landing.editor_state.last_seen_utc, EXCLUDED.last_seen_utc),
    edits_seen        = EXCLUDED.edits_seen,
    reverts_performed = EXCLUDED.reverts_performed,
    edits_reverted    = EXCLUDED.edits_reverted
"""

PERSIST_PAGE_SQL = """
INSERT INTO landing.page_state
    (page_key, first_seen_utc, last_seen_utc, edits_seen, edits_reverted)
VALUES (%(key)s, %(first)s, %(last)s, %(edits)s, %(reverted)s)
ON CONFLICT (page_key) DO UPDATE SET
    first_seen_utc = LEAST(landing.page_state.first_seen_utc, EXCLUDED.first_seen_utc),
    last_seen_utc  = GREATEST(landing.page_state.last_seen_utc, EXCLUDED.last_seen_utc),
    edits_seen     = EXCLUDED.edits_seen,
    edits_reverted = EXCLUDED.edits_reverted
"""


def persist(conn: Any, state: dict[str, Any]) -> tuple[int, int]:
    editors = [
        {
            "key": key,
            "first": v["first"],
            "last": v["last"],
            "edits": v["edits"],
            "reverts": v["reverts_performed"],
            "reverted": v["edits_reverted"],
        }
        for key, v in state.get("editors", {}).items()
    ]
    pages = [
        {
            "key": key,
            "first": v["first"],
            "last": v["last"],
            "edits": v["edits"],
            "reverted": v["reverted"],
        }
        for key, v in state.get("pages", {}).items()
    ]
    with conn.cursor() as cur:
        if editors:
            cur.executemany(PERSIST_EDITOR_SQL, editors)
        if pages:
            cur.executemany(PERSIST_PAGE_SQL, pages)
        # GREATEST, so a partial batch can never move the frontier backwards.
        # An id that has been seen has been seen.
        if state.get("max_user_id"):
            cur.execute(PERSIST_FRONTIER_SQL, {"value": state["max_user_id"]})
    return len(editors), len(pages)


def run(*, days: int = 30) -> dict[str, Any]:
    run_id = new_run_id()
    with connect() as lock_conn, advisory_lock(lock_conn, STATE_LOCK_KEY) as acquired:
        if not acquired:
            print("state: another run holds the lock, exiting cleanly")
            return {"skipped": True}

        with RunContext(run_id, job=JOB) as run_ctx, connect() as conn:
            result = replay(conn, days=days)
            editors, pages = persist(conn, result["state"])
            run_ctx.rows_read = result["events"]
            run_ctx.rows_written = editors + pages

    cov = result["coverage"]
    pct = 100.0 * cov["with_history"] / cov["scored"] if cov["scored"] else 0.0
    print(f"state: replayed {result['events']:,} events -> {editors:,} editors, {pages:,} pages")
    print(f"state: {pct:.1f}% of events had ANY prior editor history")
    for stratum, b in sorted(cov["by_stratum"].items()):
        share = 100.0 * b["with_history"] / b["scored"] if b["scored"] else 0.0
        verdict = "usable" if share >= 10 else "UNINFORMATIVE (M2-FR-13)"
        seen, total = b["with_history"], b["scored"]
        print(f"  {stratum:<12}{seen:>7,} of {total:>7,}  {share:>5.1f}%  {verdict}")

    return {"editors": editors, "pages": pages, "coverage": cov}


def main() -> int:
    # Registered before any work, so a run that dies mid-read still
    # accounts for what it spent.
    record_on_exit("state")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument(
        "--apply-reverts",
        action="store_true",
        help="fold newly discovered reverts into persisted counters, and nothing else",
    )
    args = parser.parse_args()

    if args.apply_reverts:
        with connect() as lock_conn, advisory_lock(lock_conn, STATE_LOCK_KEY) as acquired:
            if not acquired:
                print("state: another run holds the lock, exiting cleanly")
                return 0
            run_id = new_run_id()
            with RunContext(run_id, job="apply_reverts") as ctx, connect() as conn:
                result = apply_reverts(conn, days=args.days)
                ctx.rows_read = result["applied"]
                ctx.rows_written = result["counters_moved"]
        print(
            f"state: folded {result['applied']:,} newly discovered reverts "
            f"({result['counters_moved']:,} moved a counter, "
            f"{result['no_row_to_move']:,} had no row online)"
        )
        return 0

    run(days=args.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
