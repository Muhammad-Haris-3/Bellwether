"""Score newly ingested edits, before their outcome exists (M3-FR-4 to FR-11).

The point of the milestone. Everything M0 built to guarantee the outcome was
honestly observed is worth nothing if the prediction can be written afterwards,
so this job commits a score to an append-only register the moment an edit is
ingested and long before anybody knows what happened to it.

**Ordering.** Events are scored in `event_ts` order, and each is folded into
state only AFTER its own score is emitted. Identical to the replay in
`state.py`, deliberately, because they are the same two functions — one
implementation, so there is nothing to drift.

**State is loaded for the batch, not wholesale.** At steady state the tables
hold tens of thousands of editors and pages, and reading all of them every ten
minutes to score a few dozen events would be absurd. Only the keys appearing in
the batch are read, which is also what makes the same code usable for a single
event in future.
"""

from __future__ import annotations

import argparse
import pickle
import sys
from typing import Any

from bellwether import features, knowability, registry, state
from bellwether.db import advisory_lock, connect
from bellwether.runlog import RunContext, new_run_id, utcnow

JOB = "score"
SCORE_LOCK_KEY = 815_007

UNSCORED_SQL = """
SELECT e.revid, e.old_revid, e.event_ts, e.ns, e.title, e.user_name, e.user_id,
       e.is_anon, e.is_temp, e.is_minor, e.is_bot, e.comment, e.comment_hidden,
       e.oldlen, e.newlen, e.tags, e.sampling_stratum,
       (e.tags && ARRAY['mw-undo','mw-rollback','mw-manual-revert']) AS is_reverting,
       -- M3-FR-10. Was a revert for this edit ALREADY visible when we got here?
       --
       -- If the scorer falls far enough behind it will eventually score an edit
       -- that has already been reverted. Nothing raises. The score is simply
       -- trivially correct and accuracy improves for the worst possible reason.
       EXISTS (SELECT 1 FROM outcome.revert_events r
                WHERE r.reverted_revid = e.revid AND r.revert_ts <= now())
           AS outcome_already_observable
  FROM landing.rc_events e
 WHERE NOT EXISTS (
           SELECT 1 FROM register.predictions p
            WHERE p.revid = e.revid
              AND p.model_version = %(model_version)s
              AND p.role = 'champion')
   AND e.event_ts >= now() - make_interval(days => %(lookback_days)s)
 ORDER BY e.event_ts, e.revid
 LIMIT %(limit)s
"""

INSERT_PREDICTION_SQL = """
INSERT INTO register.predictions
    (revid, event_ts, scored_at, model_version, role, score, feature_hash,
     outcome_observable_at_scoring, scored_by_run)
VALUES (%(revid)s, %(event_ts)s, %(scored_at)s, %(model_version)s, 'champion',
        %(score)s, %(feature_hash)s, %(observable)s, %(run_id)s)
ON CONFLICT (revid, model_version, role) DO NOTHING
"""


def run(*, limit: int = 5_000, lookback_days: int = 3) -> dict[str, Any]:
    run_id = new_run_id()
    knowability.run_all()

    with connect() as lock_conn, advisory_lock(lock_conn, SCORE_LOCK_KEY) as acquired:
        if not acquired:
            print("score: another run holds the lock, exiting cleanly")
            return {"skipped": True}

        with connect() as conn:
            champion = registry.champion(conn)
        if not champion:
            print("score: no model registered. Train one first.")
            return {"skipped": True, "reason": "no champion"}

        version = champion["model_version"]
        # Before loading, never after. A model that has already scored cannot
        # be un-scored, so the digest is checked while refusing is still cheap.
        artifact = registry.verify(version, champion["artifact_sha256"])
        with artifact.open("rb") as fh:
            model = pickle.load(fh)  # noqa: S301 - hash-verified above

        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                UNSCORED_SQL,
                {"model_version": version, "limit": limit, "lookback_days": lookback_days},
            )
            events = cur.fetchall()

        if not events:
            print(f"score: nothing unscored for {version}")
            return {"scored": 0, "model_version": version}

        with RunContext(run_id, job=JOB) as ctx, connect() as conn:
            st = state.load_for(conn, events)
            names = features.feature_names()
            rows, late = [], 0
            now = utcnow()

            for event in events:
                vector = features.build(event, state.history_for(st, event))
                score = float(model.predict_proba([[vector[n] for n in names]])[0][1])
                if event["outcome_already_observable"]:
                    late += 1
                rows.append(
                    {
                        "revid": event["revid"],
                        "event_ts": event["event_ts"],
                        "scored_at": now,
                        "model_version": version,
                        "score": score,
                        "feature_hash": features.feature_hash(vector),
                        "observable": event["outcome_already_observable"],
                        "run_id": run_id,
                    }
                )
                # After the score, never before.
                state.observe(st, event)

            with conn.cursor() as cur:
                cur.executemany(INSERT_PREDICTION_SQL, rows)
                written = max(cur.rowcount, 0)
            state.persist(conn, st)

            ctx.rows_read = len(events)
            ctx.rows_written = written
            ctx.partial = late > 0

    lags = [(now - e["event_ts"]).total_seconds() / 60 for e in events]
    lags.sort()
    print(f"score: {written:,} scored with {version}")
    print(f"  lag minutes: median {lags[len(lags) // 2]:.1f}  max {lags[-1]:.1f}")
    if late:
        print(
            f"  {late:,} scored AFTER their outcome was already observable - "
            f"flagged, and excluded from every accuracy claim"
        )

    return {
        "scored": written,
        "model_version": version,
        "late": late,
        "median_lag_minutes": lags[len(lags) // 2],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5_000)
    parser.add_argument("--lookback-days", type=int, default=3)
    args = parser.parse_args()
    run(limit=args.limit, lookback_days=args.lookback_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
