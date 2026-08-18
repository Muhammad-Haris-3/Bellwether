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
from bellwether.usage import record_on_exit

JOB = "score"
SCORE_LOCK_KEY = 815_007

UNSCORED_SQL = """
SELECT e.revid, e.old_revid, e.event_ts, e.ns, e.title, e.user_name, e.user_id,
       e.is_anon, e.is_temp, e.is_minor, e.is_bot, e.comment, e.comment_hidden,
       e.oldlen, e.newlen, e.tags, e.sampling_stratum,
       (e.tags && ARRAY['mw-undo','mw-rollback','mw-manual-revert']) AS is_reverting,
       -- M3-FR-10. Was this edit's outcome ALREADY available when we got here?
       --
       -- If the scorer falls far enough behind it will eventually score an edit
       -- that has already been reverted. Nothing raises. The score is simply
       -- trivially correct and accuracy improves for the worst possible reason.
       --
       -- Two DIFFERENT ways that happens, so both limbs are checked:
       --
       --   revert_events  the revert HAPPENED before we scored, whether or not
       --                  we knew. Information about it could have reached the
       --                  features through page and editor state.
       --   labels         we ALREADY HELD the answer. Nothing is being
       --                  predicted; the row is a lookup wearing a score.
       --
       -- Only the first was checked until now, and it covers the smaller path
       -- by far: revert_events is built from reverting edits parsed out of edit
       -- summaries, while most outcomes arrive as mw-reverted tags that land in
       -- outcome.labels and never produce a revert_events row at all. The guard
       -- was blind to the majority of the outcomes it exists to catch.
       (EXISTS (SELECT 1 FROM outcome.revert_events r
                 WHERE r.reverted_revid = e.revid AND r.revert_ts <= now())
        OR EXISTS (SELECT 1 FROM outcome.labels l
                    WHERE l.revid = e.revid AND l.first_observed_at_utc <= now()))
           AS outcome_already_observable
  FROM landing.rc_events e
 WHERE NOT EXISTS (
           SELECT 1 FROM register.predictions p
            WHERE p.revid = e.revid
              AND p.model_version = %(model_version)s
              AND p.role = 'champion')
   AND e.event_ts >= now() - make_interval(days => %(lookback_days)s)
   -- Never score what the champion was fitted to.
   --
   -- The lookback window and the training window are set independently and
   -- nothing stopped them overlapping. Where they do, the scorer would write
   -- predictions on edits the model has memorised into the register that exists
   -- to measure how it does on edits it has never seen. This champion scores
   -- 0.686 PR-AUC in-sample against 0.256 out-of-sample, so the contamination
   -- would not be subtle. It has not happened yet only because an ingestion gap
   -- happens to sit between the two windows, which is luck, not a guarantee.
   AND NOT (e.event_ts >= %(training_start)s AND e.event_ts < %(training_end)s)
 ORDER BY e.event_ts, e.revid
 LIMIT %(limit)s
"""

INSERT_PREDICTION_SQL = """
INSERT INTO register.predictions
    (revid, event_ts, scored_at, model_version, role, score, feature_hash,
     outcome_observable_at_scoring, scored_by_run)
VALUES (%(revid)s, %(event_ts)s, %(scored_at)s, %(model_version)s, %(role)s,
        %(score)s, %(feature_hash)s, %(observable)s, %(run_id)s)
ON CONFLICT (revid, model_version, role) DO NOTHING
"""


def _load(model: dict[str, Any]) -> Any:
    """Verify the digest, then unpickle. Never the other way round.

    A model that has already scored cannot be un-scored, so the check happens
    while refusing is still cheap.
    """
    artifact = registry.verify(model["model_version"], model["artifact_sha256"])
    with artifact.open("rb") as fh:
        return pickle.load(fh)  # noqa: S301 - hash-verified above


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
        model = _load(champion)

        # M5-FR-7. The challenger scores the same events, in the same run, from
        # the same state — one scorer with two model versions, never a second
        # implementation. Train/serve skew took three modules to find in M3; a
        # separate shadow scorer would be the same mistake, made on purpose.
        with connect() as conn:
            shadow = registry.challenger(conn, version)
        shadow_model = _load(shadow) if shadow else None
        if shadow:
            print(f"score: shadowing {shadow['model_version']}")

        # Each model is scored against the feature list it was REGISTERED with,
        # not against whatever this code happens to produce today.
        #
        # Without this the feature set cannot change, whatever PREREGISTRATION
        # §11 says. A challenger carrying one new feature would be handed the
        # champion's row, raise on every event, be swallowed as "no opinion"
        # below, and accumulate zero paired observations — so P-3 could never be
        # reached and it would read as a broken model rather than an
        # incompatible one. The champion is worse: its predict_proba is not
        # guarded, so the first run after any feature change takes scoring down.
        available = set(features.feature_names())
        champion_names = list(champion["feature_names"])
        unavailable = [n for n in champion_names if n not in available]
        if unavailable:
            # Refuse rather than improvise. Scoring the champion on a feature
            # set it was not trained on writes numbers that are wrong into a
            # register that cannot delete them, and a failed run is recoverable
            # in a way that a poisoned register is not.
            print(
                f"::error title=Feature mismatch::champion {version} needs "
                f"{unavailable}, which this build does not produce. Nothing was "
                "scored. Retrain, or deploy the code the champion was trained with."
            )
            return {"skipped": True, "reason": "champion feature mismatch"}

        shadow_names: list[str] = []
        if shadow and shadow_model is not None:
            shadow_names = list(shadow["feature_names"])
            missing_for_shadow = [n for n in shadow_names if n not in available]
            if missing_for_shadow:
                # Dropped once, loudly, instead of failing per event. The
                # per-event handler below exists for a challenger that is
                # unstable on some inputs; a challenger this build cannot feed
                # at all is a deployment error, and counting it five thousand
                # times as "no opinion" would bury the reason.
                print(
                    f"::warning title=Shadow incompatible::challenger "
                    f"{shadow['model_version']} needs {missing_for_shadow}, which "
                    "this build does not produce. Not shadowed this run."
                )
                shadow, shadow_model, shadow_names = None, None, []

        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                UNSCORED_SQL,
                {
                    "model_version": version,
                    "limit": limit,
                    "lookback_days": lookback_days,
                    "training_start": champion["training_start"],
                    "training_end": champion["training_end"],
                },
            )
            events = cur.fetchall()

        if not events:
            print(f"score: nothing unscored for {version}")
            return {"scored": 0, "model_version": version}

        with RunContext(run_id, job=JOB) as ctx, connect() as conn:
            st = state.load_for(conn, events)
            rows, late, shadow_failed = [], 0, 0
            now = utcnow()

            # Which of these are ALREADY counted in the state just loaded.
            #
            # An event can reach this loop twice. UNSCORED_SQL gates on
            # model_version, so a champion change makes every event in the
            # lookback window eligible again for the new champion — correctly,
            # because the new model does need a prediction for them. What must
            # not happen twice is the FOLD: reconcile measured editor.edits at
            # exactly double for editors active across the one promotion this
            # project has had, because observe ran again on the same events and
            # persist wrote an absolute count seeded from the inflated row.
            #
            # Scoring is unaffected. Only the counter update is skipped.
            already = state.already_folded(conn, [e["revid"] for e in events])
            folded: list[int] = []
            if already:
                print(f"score: {len(already):,} events already in state, not folding again")

            for event in events:
                # Built ONCE and selected from twice. Rebuilding it per model
                # would be two chances to disagree about the same event, and the
                # paired comparison the promotion rule runs on assumes they
                # cannot. Selecting a subset of one vector cannot disagree with
                # itself, so the invariant survives models with different
                # feature sets.
                vector = features.build(event, state.history_for(st, event))
                base = {
                    "revid": event["revid"],
                    "event_ts": event["event_ts"],
                    "scored_at": now,
                    "observable": event["outcome_already_observable"],
                    "run_id": run_id,
                }

                if event["outcome_already_observable"]:
                    late += 1
                # The hash digests exactly what THIS model consumed, so a
                # prediction stays reproducible when the feature set later grows.
                # Hashing the whole vector would make every historical row
                # unreproducible the moment a feature is added.
                champion_row = [vector[n] for n in champion_names]
                rows.append(
                    {
                        **base,
                        "model_version": version,
                        "role": "champion",
                        "feature_hash": features.feature_hash(vector, champion_names),
                        "score": float(model.predict_proba([champion_row])[0][1]),
                    }
                )

                if shadow_model is not None and shadow is not None:
                    try:
                        shadow_row = [vector[n] for n in shadow_names]
                        shadow_score = float(shadow_model.predict_proba([shadow_row])[0][1])
                    except Exception:  # noqa: BLE001 - any failure means "no opinion"
                        # M5-FR-10. A challenger that errors on an event simply
                        # has no score for it, and the pairing drops that event.
                        # Recording a failure as a loss would let an unstable
                        # model hide behind a mediocre metric — and would make
                        # the champion look better the more often the
                        # challenger broke.
                        shadow_failed += 1
                    else:
                        rows.append(
                            {
                                **base,
                                "model_version": shadow["model_version"],
                                "role": "shadow",
                                "feature_hash": features.feature_hash(vector, shadow_names),
                                "score": shadow_score,
                            }
                        )

                # After the score, never before — and once per event, ever.
                if event["revid"] not in already:
                    state.observe(st, event)
                    folded.append(event["revid"])

            with conn.cursor() as cur:
                cur.executemany(INSERT_PREDICTION_SQL, rows)
                written = max(cur.rowcount, 0)
            state.persist(conn, st)
            # Same transaction as the persist it describes. Split across two, a
            # crash between them leaves the counters moved and the ledger
            # silent, and the next run folds these events again.
            state.record_folded(conn, folded, source="score")

            ctx.rows_read = len(events)
            ctx.rows_written = written
            ctx.partial = late > 0

    lags = [(now - e["event_ts"]).total_seconds() / 60 for e in events]
    lags.sort()
    # Rows written, not events scored: with a challenger in shadow there are two
    # per event, and reporting the total as "scored with <champion>" would
    # double the champion's apparent throughput.
    print(f"score: {len(events):,} events, {written:,} rows, champion {version}")
    print(f"  lag minutes: median {lags[len(lags) // 2]:.1f}  max {lags[-1]:.1f}")
    if late:
        print(
            f"  {late:,} scored AFTER their outcome was already observable - "
            f"flagged, and excluded from every accuracy claim"
        )
    if shadow:
        print(f"  shadow {shadow['model_version']}: {len(events) - shadow_failed:,} scored")
        if shadow_failed:
            print(f"  {shadow_failed:,} events the challenger could not score - excluded, not lost")

    return {
        "scored": written,
        "model_version": version,
        "late": late,
        "median_lag_minutes": lags[len(lags) // 2],
    }


def main() -> int:
    # Registered before any work, so a run that dies mid-read still
    # accounts for what it spent.
    record_on_exit("score")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5_000)
    parser.add_argument("--lookback-days", type=int, default=3)
    args = parser.parse_args()
    run(limit=args.limit, lookback_days=args.lookback_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
