"""What causes a retrain (M5-FR-11 to FR-14).

Three conditions, quoted from `PREREGISTRATION.md` §8 and held in
`bellwether/preregistration.py`:

    decay   rolling 7-day PR-AUC below the champion's registered baseline by
            more than 0.03, on 3 consecutive daily windows
    drift   PSI above 0.20 on any monitored feature or on the score
            distribution, on 3 consecutive daily windows
    floor   7 days since the last training run

**Every evaluation is recorded, including the ones that fire nothing.** A table
holding only firings cannot answer "was this checked yesterday", and a trigger
that silently stopped being evaluated looks exactly like one that keeps not
firing — which is the failure this project has now found four times in other
guises.

**Three consecutive windows means three consecutive DAYS.** Not "the last three
rows". GitHub's cron is best-effort, so evaluations can be missed, and treating
the last three rows as three consecutive days would let a trigger fire on
evidence spanning a week. The streak is stored on the row and a gap resets it
(M5-FR-12).

**PSI is measured against the training distribution, not a recent window.**
Whether today resembles yesterday is not the question. The question is whether
today resembles what the model was fitted to (M5-FR-13).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
from sklearn.metrics import average_precision_score

from bellwether import features, metrics, registry, state
from bellwether import preregistration as pre
from bellwether.config import get_settings
from bellwether.db import advisory_lock, connect
from bellwether.runlog import RunContext, new_run_id
from bellwether.schema import require_current
from bellwether.usage import record_on_exit

JOB = "triggers"
TRIGGERS_LOCK_KEY = 815_012

# Which inputs are watched for drift.
#
# `PREREGISTRATION.md` §8 says "any monitored feature" and leaves the set to be
# defined; this is that definition, and changing it is an amendment.
#
# The features the model measurably uses, from M4's permutation importance, plus
# the score distribution itself. Monitoring all 28 would include the twelve that
# measure at exactly 0.000 — drift in an input the model ignores cannot change
# its output, so firing a retrain on it would be retraining on noise, which is
# the thing three-consecutive-windows exists to prevent.
MONITORED_FEATURES: tuple[str, ...] = (
    "account_newness",
    "editor_edits_seen",
    "editor_days_known",
    "byte_delta",
    "abs_byte_delta",
    "tag_count",
    "comment_length",
    "is_logged_out",
    "editor_reverts_performed",
    "page_edits_seen",
    "is_mobile",
    "is_visual_editor",
)

PSI_BINS = 10

# M5-FR-16. The training window is a stated function of the trigger date, not a
# choice made per run — a window picked per retrain is a hyperparameter chosen
# after seeing the data, wearing a schedule's clothes.
#
# It ends one maturity horizon back, because training on events whose outcome is
# not yet known would fill the positives class from whatever happened to be
# labelled early.
TRAIN_WINDOW_DAYS = 30
TRAIN_WINDOW_LAG_DAYS = 7


def training_window(window_day: date) -> tuple[datetime, datetime]:
    end = datetime.combine(window_day, datetime.min.time(), tzinfo=UTC) - timedelta(
        days=TRAIN_WINDOW_LAG_DAYS
    )
    return end - timedelta(days=TRAIN_WINDOW_DAYS), end


# Matured champion predictions in the rolling window, with the features drift is
# measured over. The maturity rule is M4's: elapsed time since the EDIT applied
# to both classes alike, and the outcome determined by either arm.
ROLLING_SQL = """
WITH observed AS (
    SELECT c.revid, max(c.age_seconds) AS last_observed_age,
           bool_or(c.had_reverted_tag) AS ever_positive
      FROM outcome.label_checks c GROUP BY c.revid
)
SELECT p.revid, p.score, e.event_ts,
       e.old_revid, e.ns, e.title, e.user_name, e.user_id, e.is_anon, e.is_temp,
       e.is_minor, e.is_bot, e.comment, e.comment_hidden, e.oldlen, e.newlen,
       e.tags, e.sampling_stratum,
       (e.tags && ARRAY['mw-undo','mw-rollback','mw-manual-revert']) AS is_reverting,
       (o.ever_positive
        OR EXISTS (SELECT 1 FROM outcome.labels l
                    WHERE l.revid = p.revid AND l.label))  AS label
  FROM register.predictions p
  JOIN landing.rc_events e ON e.revid = p.revid
  JOIN observed o          ON o.revid = p.revid
 WHERE p.role = 'champion'
   AND p.model_version = %(version)s
   AND NOT p.outcome_observable_at_scoring
   AND p.event_ts >= %(window_start)s
   AND p.event_ts <  %(window_end)s
   AND EXTRACT(epoch FROM now() - p.event_ts) >= %(maturity)s
   AND (o.ever_positive OR o.last_observed_age >= %(maturity)s)
 ORDER BY p.event_ts, p.revid
"""

# The reference distribution: the events the champion was fitted to.
#
# Recomputed from rc_events rather than stored. That is sufficient while the
# training window is inside the 30-day raw retention and stops being possible
# afterwards — at which point PSI is recorded as unavailable with a reason
# rather than quietly becoming zero. A stored reference is the eventual fix; a
# silently disabled drift detector is the thing to avoid in the meantime.
REFERENCE_SQL = """
SELECT e.revid, e.old_revid, e.event_ts, e.ns, e.title, e.user_name, e.user_id,
       e.is_anon, e.is_temp, e.is_minor, e.is_bot, e.comment, e.comment_hidden,
       e.oldlen, e.newlen, e.tags, e.sampling_stratum,
       (e.tags && ARRAY['mw-undo','mw-rollback','mw-manual-revert']) AS is_reverting
  FROM landing.rc_events e
 WHERE e.event_ts >= %(start)s AND e.event_ts < %(end)s
 ORDER BY e.event_ts, e.revid
"""

# The reference SCORE distribution: what this champion looked like when it
# started serving.
#
# Not its scores on the training data — those are in-sample, and comparing them
# against live scores would measure the generalisation gap and call it drift.
# In-sample 0.729 against out-of-sample 0.256 is how large that mistake would
# have been.
#
# Its own first week of live scoring is the honest reference: this is what the
# model looked like when it was working.
REFERENCE_SCORES_SQL = """
SELECT p.score
  FROM register.predictions p
  JOIN register.model_registry m ON m.model_version = p.model_version
 WHERE p.role = 'champion'
   AND p.model_version = %(version)s
   AND p.scored_at < m.trained_at + make_interval(days => %(days)s)
"""

PREVIOUS_SQL = """
SELECT window_day, decay_streak, drift_streak
  FROM decide.trigger_evaluations
 WHERE champion_version = %(version)s
 ORDER BY window_day DESC
 LIMIT 1
"""

# The champion's registered baseline.
#
# Its measured live PR-AUC at the moment it was promoted. For a champion that
# predates any promotion — M3's first, installed by recency — there is no such
# figure, so its FIRST recorded rolling measurement becomes the baseline. That is
# the honest reading of "registered baseline" for a model the rule never passed,
# and it means a model that started poorly and stayed poor never trips decay,
# which is correct: decay means got worse, not was never good.
BASELINE_SQL = """
SELECT COALESCE(
    (SELECT d.challenger_pr_auc FROM decide.model_decisions d
      WHERE d.decision = 'promote' AND d.challenger_version = %(version)s
      ORDER BY d.decided_at DESC LIMIT 1),
    (SELECT t.rolling_pr_auc FROM decide.trigger_evaluations t
      WHERE t.champion_version = %(version)s AND t.rolling_pr_auc IS NOT NULL
      ORDER BY t.window_day ASC LIMIT 1)
) AS baseline
"""

LAST_TRAIN_SQL = "SELECT max(trained_at) AS at FROM register.model_registry"

INSERT_EVALUATION_SQL = """
INSERT INTO decide.trigger_evaluations
    (window_day, champion_version, rolling_pr_auc, baseline_pr_auc, pr_auc_drop,
     max_psi, max_psi_feature, days_since_train, decay_breached, drift_breached,
     floor_breached, decay_streak, drift_streak, fired, fired_reason, n_matured,
     code_commit, run_id)
VALUES (%(window_day)s, %(version)s, %(rolling)s, %(baseline)s, %(drop)s,
        %(max_psi)s, %(max_psi_feature)s, %(days_since_train)s, %(decay)s,
        %(drift)s, %(floor)s, %(decay_streak)s, %(drift_streak)s, %(fired)s,
        %(reason)s, %(n_matured)s, %(commit)s, %(run_id)s)
ON CONFLICT (window_day, champion_version) DO NOTHING
RETURNING evaluation_id
"""

INSERT_PSI_SQL = """
INSERT INTO decide.psi_features (evaluation_id, feature, psi)
VALUES (%(evaluation_id)s, %(feature)s, %(psi)s)
ON CONFLICT (evaluation_id, feature) DO NOTHING
"""


def rolling_pr_auc(
    conn: Any, *, version: str, day: date
) -> tuple[float | None, list[dict[str, Any]]]:
    """The champion's PR-AUC over the rolling window ending on `day`.

    Shared with the rollback check, which asks the same question of a newly
    promoted model. Two implementations of "how is it doing lately" would
    eventually disagree, and the one that decides a rollback is not the one to
    let drift.

    Returns None when the window holds one class or nothing — undefined rather
    than zero, because a window with no positives is not evidence of a model
    performing badly.

    **The window is over the last seven days of EVIDENCE, not of events.** A
    window over recent events is empty by construction: maturity requires an
    event to be seven days old, and the last seven days contains nothing that
    old. The first version windowed on `event_ts` and every rolling figure it
    produced was None, which would have disabled the decay trigger and the
    rollback check together, silently, while both kept reporting that they had
    run.
    """
    maturity = metrics.PROVISIONAL_MATURITY_SECONDS
    matured_by = datetime.combine(day, datetime.min.time(), tzinfo=UTC) - timedelta(
        seconds=maturity
    )
    with conn.cursor() as cur:
        cur.execute(
            ROLLING_SQL,
            {
                "version": version,
                "window_start": matured_by - timedelta(days=pre.ROLLING_WINDOW_DAYS),
                "window_end": matured_by,
                "maturity": maturity,
            },
        )
        matured = cur.fetchall()

    labels = np.asarray([1 if r["label"] else 0 for r in matured], dtype=int)
    if not len(matured) or labels.sum() in (0, len(labels)):
        return None, matured
    scores = np.asarray([float(r["score"]) for r in matured], dtype=float)
    return round(float(average_precision_score(labels, scores)), 6), matured


def psi(reference: np.ndarray, current: np.ndarray, *, bins: int = PSI_BINS) -> float | None:
    """Population stability index, with bin edges taken from the REFERENCE.

    Edges from the reference and not from the pooled data, because pooled edges
    move when the current window moves — the measurement would then partly
    describe its own bins. This is the same reasoning that makes M4's
    calibration bins equal-width rather than quantile.

    Returns None when either side is too small to say anything, rather than a
    number that looks like a measurement.
    """
    reference = reference[np.isfinite(reference)]
    current = current[np.isfinite(current)]
    if len(reference) < bins * 5 or len(current) < bins * 5:
        return None

    # A constant reference cannot drift in any way this measures, and reporting
    # 0.0 would be indistinguishable from having checked it.
    #
    # Judged on distinct VALUES rather than on the number of bin edges. Both a
    # constant and a binary feature collapse to two bins, and binary features —
    # is_logged_out, is_mobile — are exactly the ones where a shift in the
    # proportion is the drift worth catching.
    if len(np.unique(reference)) < 2:
        return None

    edges = np.quantile(reference, np.linspace(0, 1, bins + 1))
    edges[0], edges[-1] = -np.inf, np.inf
    edges = np.unique(edges)

    ref_counts, _ = np.histogram(reference, bins=edges)
    cur_counts, _ = np.histogram(current, bins=edges)

    # Laplace smoothing. An empty bin on one side alone would send a term to
    # infinity, and one unseen value would fire a retrain on its own.
    ref_share = (ref_counts + 1) / (ref_counts.sum() + len(ref_counts))
    cur_share = (cur_counts + 1) / (cur_counts.sum() + len(cur_counts))
    return float(np.sum((cur_share - ref_share) * np.log(cur_share / ref_share)))


def _matrix(rows: list[dict[str, Any]]) -> dict[str, np.ndarray]:
    """Feature columns for a set of events, folded in time order.

    The same read-emit-fold ordering as everywhere else. Drift measured on
    features built any other way would be drift in the builder.
    """
    st: dict[str, Any] = {}
    columns: dict[str, list[float]] = {name: [] for name in MONITORED_FEATURES}
    for row in rows:
        vector = features.build(row, state.history_for(st, row))
        for name in MONITORED_FEATURES:
            columns[name].append(float(vector[name]))
        state.observe(st, row)
    return {name: np.asarray(values, dtype=float) for name, values in columns.items()}


def run(*, window_day: date | None = None, retrain: bool = True) -> dict[str, Any]:
    run_id = new_run_id()
    settings = get_settings()
    day = window_day or datetime.now(UTC).date()

    with connect() as lock_conn, advisory_lock(lock_conn, TRIGGERS_LOCK_KEY) as acquired:
        if not acquired:
            print("triggers: another run holds the lock, exiting cleanly")
            return {"skipped": True}

        with connect() as conn:
            require_current(conn)
            champion = registry.champion(conn)
            if not champion:
                print("triggers: no champion. Nothing to evaluate.")
                return {"skipped": True, "reason": "no champion"}
            version = champion["model_version"]

            rolling, matured = rolling_pr_auc(conn, version=version, day=day)

            with conn.cursor() as cur:
                cur.execute(BASELINE_SQL, {"version": version})
                baseline = (cur.fetchone() or {}).get("baseline")

                cur.execute(PREVIOUS_SQL, {"version": version})
                previous = cur.fetchone()

                cur.execute(LAST_TRAIN_SQL)
                last_train = (cur.fetchone() or {}).get("at")

                cur.execute(
                    REFERENCE_SQL,
                    {"start": champion["training_start"], "end": champion["training_end"]},
                )
                reference = cur.fetchall()

                cur.execute(
                    REFERENCE_SCORES_SQL,
                    {"version": version, "days": pre.ROLLING_WINDOW_DAYS},
                )
                reference_scores = np.asarray(
                    [float(r["score"]) for r in cur.fetchall()], dtype=float
                )

        # --- decay ---------------------------------------------------------
        drop = None
        decay = False
        if rolling is not None and baseline is not None:
            drop = round(float(baseline) - rolling, 6)
            decay = drop > pre.DECAY_PR_AUC_DROP

        # --- drift ---------------------------------------------------------
        per_feature: dict[str, float] = {}
        if reference and matured:
            ref_columns = _matrix(reference)
            cur_columns = _matrix(matured)
            for name in MONITORED_FEATURES:
                value = psi(ref_columns[name], cur_columns[name])
                if value is not None:
                    per_feature[name] = round(value, 6)
        # The score distribution is monitored alongside the inputs: a model
        # whose outputs have shifted while every input looks stable is drifting
        # in a way no feature check would see.
        if len(reference_scores) and matured:
            score_psi = psi(
                reference_scores,
                np.asarray([float(r["score"]) for r in matured], dtype=float),
            )
            if score_psi is not None:
                per_feature["__score__"] = round(score_psi, 6)

        max_psi_feature = max(per_feature, key=lambda k: per_feature[k]) if per_feature else None
        max_psi = per_feature[max_psi_feature] if max_psi_feature else None
        drift = bool(max_psi is not None and max_psi > pre.DRIFT_PSI_THRESHOLD)

        # --- floor ---------------------------------------------------------
        days_since_train = None
        if last_train is not None:
            days_since_train = int((datetime.now(UTC) - last_train).total_seconds() // 86400)
        floor = bool(days_since_train is not None and days_since_train >= pre.RETRAIN_FLOOR_DAYS)

        # --- streaks -------------------------------------------------------
        #
        # M5-FR-12. Consecutive DAYS, not consecutive rows. A missed evaluation
        # resets the count, because three rows spanning a week is not three
        # consecutive windows and would let a trigger fire on evidence that was
        # never continuous.
        carried = (
            previous if previous and previous["window_day"] == day - timedelta(days=1) else None
        )
        decay_streak = ((carried["decay_streak"] if carried else 0) + 1) if decay else 0
        drift_streak = ((carried["drift_streak"] if carried else 0) + 1) if drift else 0

        reasons = []
        if decay_streak >= pre.TRIGGER_CONSECUTIVE_WINDOWS:
            reasons.append(f"decay: PR-AUC down {drop} for {decay_streak} days")
        if drift_streak >= pre.TRIGGER_CONSECUTIVE_WINDOWS:
            reasons.append(f"drift: PSI {max_psi} on {max_psi_feature} for {drift_streak} days")
        if floor:
            reasons.append(f"floor: {days_since_train} days since the last training run")
        fired = bool(reasons)

        with RunContext(run_id, job=JOB) as ctx, connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    INSERT_EVALUATION_SQL,
                    {
                        "window_day": day,
                        "version": version,
                        "rolling": rolling,
                        "baseline": baseline,
                        "drop": drop,
                        "max_psi": max_psi,
                        "max_psi_feature": max_psi_feature,
                        "days_since_train": days_since_train,
                        "decay": decay,
                        "drift": drift,
                        "floor": floor,
                        "decay_streak": decay_streak,
                        "drift_streak": drift_streak,
                        "fired": fired,
                        "reason": "; ".join(reasons) or None,
                        "n_matured": len(matured),
                        "commit": settings.build_id,
                        "run_id": run_id,
                    },
                )
                inserted = cur.fetchone()
                if inserted and per_feature:
                    cur.executemany(
                        INSERT_PSI_SQL,
                        [
                            {
                                "evaluation_id": inserted["evaluation_id"],
                                "feature": name,
                                "psi": value,
                            }
                            for name, value in per_feature.items()
                        ],
                    )
            ctx.rows_read = len(matured)
            ctx.rows_written = 1

    print(f"triggers: {day} champion {version}")
    print(f"  matured in window   {len(matured):,}")
    print(f"  rolling PR-AUC      {rolling}  baseline {baseline}  drop {drop}")
    print(f"  worst PSI           {max_psi} on {max_psi_feature}")
    print(f"  days since training {days_since_train}")
    print(
        f"  breached: decay={decay} ({decay_streak}/{pre.TRIGGER_CONSECUTIVE_WINDOWS})  "
        f"drift={drift} ({drift_streak}/{pre.TRIGGER_CONSECUTIVE_WINDOWS})  floor={floor}"
    )
    if fired:
        print(f"  FIRED — {'; '.join(reasons)}")
    else:
        print("  nothing fired")

    result = {
        "window_day": day.isoformat(),
        "fired": fired,
        "reasons": reasons,
        "rolling_pr_auc": rolling,
        "n_matured": len(matured),
    }

    if fired and retrain:
        # M5-FR-15. Automatic, and into shadow — never production, whatever its
        # offline metrics say. Offline and live numbers have not once agreed in
        # this project (in-sample 0.729 against out-of-sample 0.256), and a model
        # that promotes itself on the strength of them is promoting itself on
        # memorisation.
        from bellwether import train

        start, end = training_window(day)
        print(f"  retraining on [{start:%Y-%m-%d}, {end:%Y-%m-%d})")
        result["retrain"] = train.run(window_start=start.isoformat(), window_end=end.isoformat())

    return result


def main() -> int:
    # Registered before any work, so a run that dies mid-read still
    # accounts for what it spent.
    record_on_exit("triggers")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", type=str, default=None, help="window day, YYYY-MM-DD")
    parser.add_argument(
        "--no-retrain",
        action="store_true",
        help="evaluate and record only; do not act on a firing",
    )
    args = parser.parse_args()
    run(
        window_day=date.fromisoformat(args.day) if args.day else None,
        retrain=not args.no_retrain,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
