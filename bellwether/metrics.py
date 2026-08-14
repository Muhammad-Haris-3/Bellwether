"""Grade the predictions the project actually made (M4 §3 to §5).

`evaluate.py` scores an offline backtest over a backfilled census. This scores
the register: forecasts committed before the answer existed, on live data, under
a scoring lag, graded by outcomes that arrived late and unevenly. Where the two
disagree the second one is true, and both are kept so that neither can quietly
replace the other.

**Maturity is enforced in the query, not by the caller** (M4-FR-5, SRS R-3).
The filter is inside `MATURED_SQL` where it cannot be forgotten, and it is not a
clock check: an edit is matured when it has been *observed* at or beyond the
window, or is already known reverted. An event nobody has checked is not a
negative, and counting it as one would deflate every rate here for the same
reason it did in M2.

**Excluding late scores is correct and is not neutral.** M3-FR-10 flags
predictions written after their own outcome was already observable, and they are
excluded from every accuracy figure. But those are not a random sample — they
concentrate in edits that were reverted fast, so the exclusion selects on the
outcome. Its own base rate is published beside the metric it protects
(M4-FR-3), because "we excluded 4%" and "we excluded 4% that were 60% positive"
are different statements about the same number.

**Segments are diagnosis, never a headline.** The list is fixed in the M4 spec
§4 and every one is written on every run, including the ones that look bad.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from bellwether.config import get_settings
from bellwether.db import advisory_lock, connect
from bellwether.evaluate import BOOTSTRAP_RESAMPLES, SEED
from bellwether.runlog import RunContext, new_run_id
from bellwether.schema import require_current

JOB = "metrics"
METRICS_LOCK_KEY = 815_010

# Seven days, not M2's 48 hours, and the difference is not a preference.
#
# Two different quantities get called "maturity". M2's 48h describes when
# reverts stop arriving — a property of the world, estimated from the survival
# curve. This one has to be a window this pipeline has actually LOOKED at, and
# for the 90% of events outside the maturity cohort there is exactly one check,
# at the final checkpoint of seven days (M1 §5).
#
# Grading needs both, and the binding constraint is observation rather than the
# world. Using 48h here produced a sample that was 100% positive: a positive
# qualifies as soon as it is found, a non-cohort negative cannot be confirmed
# until its seven-day check, and between those two points the only gradeable
# events are the reverts. At seven days both arms become available at the same
# moment, which is what makes the sample unbiased rather than merely larger.
PROVISIONAL_MATURITY_SECONDS = 7 * 24 * 3600

# The maturity cohort is the 10% that receives the FULL checkpoint grid (M1 §5),
# so a 48h check exists for every one of them and both arms of the inclusion
# rule become available at 48 hours rather than seven days.
#
# It is a deterministic 10% bucket keyed on revid, so within the events it
# covers it is a probability sample: smaller, not skewed.
#
# It does NOT cover the whole table, and the earlier claim that it did was
# wrong. The flag is written at insert time and roughly 49,000 rows were
# inserted before that code shipped; ON CONFLICT DO NOTHING means re-ingesting
# them never corrects it, so the cohort is 0.93% of the table overall while
# recent ingest runs flag 8.5-12.7% as designed. The cohort therefore begins
# partway through and is a probability sample of events FROM THAT POINT ON.
#
# It is deliberately not backfilled even though the bucket is a pure function
# of revid. The labeller used the STORED flag to decide which events received
# the dense checkpoint grid, so a backfilled row would be marked as a cohort
# member while holding none of the 48h checks the cohort exists to provide —
# a sample that claims an observation nobody made.
#
# Published under its own population label rather than blended into the
# headline: a reader who could not tell the two apart would take the early
# number for the real one.
COHORT_MATURITY_SECONDS = 48 * 3600

POPULATIONS: dict[str, int] = {
    "all": PROVISIONAL_MATURITY_SECONDS,
    "maturity_cohort": COHORT_MATURITY_SECONDS,
}

WINDOWS: dict[str, int | None] = {"7d": 7, "30d": 30, "all": None}

CALIBRATION_BINS = 10

# The aggregate keeps the 2,000 resamples KC-2 was decided with, so the live
# figure and the backtest figure are built the same way and can be compared.
#
# Segments get 500 and no interval on the MARGIN — only on PR-AUC, which is the
# metric M4-FR-1 requires an interval for. Segments are diagnosis, and the cost
# is not theoretical: 27 metric rows times two bootstraps of 2,000 is 108,000
# ranking computations a run, and the job has ten minutes.
SEGMENT_RESAMPLES = 500

# Fixed in Bellwether_M4_Spec.md §4 BEFORE any segmented number was computed.
# Changing this list is an amendment with a date on it, not an analysis choice.
SEGMENTS: dict[str, str] = {
    "sampling_stratum": "sampling_stratum",
    "editor_has_history": "editor_has_history",
    "namespace": "namespace_group",
    "scoring_lag_bucket": "lag_bucket",
}

MATURED_SQL = """
WITH observed AS (
    SELECT c.revid,
           max(c.age_seconds)  AS last_observed_age,
           bool_or(c.had_reverted_tag) AS ever_positive
      FROM outcome.label_checks c
     GROUP BY c.revid
)
SELECT p.revid,
       p.score,
       p.scored_at,
       p.event_ts,
       p.outcome_observable_at_scoring                        AS scored_late,
       e.sampling_stratum,
       e.sampling_weight,
       (e.is_anon OR e.is_temp)                               AS is_logged_out,
       CASE WHEN e.ns = 0 THEN 'article' ELSE 'other' END     AS namespace_group,
       CASE WHEN EXTRACT(epoch FROM p.scored_at - p.event_ts) <= 1800
            THEN 'fast' ELSE 'slow' END                       AS lag_bucket,
       CASE WHEN COALESCE(s.edits_seen, 0) > 1 THEN 'yes' ELSE 'no' END
                                                              AS editor_has_history,
       lw.score                                               AS liftwing_score,
       (o.ever_positive
        OR EXISTS (SELECT 1 FROM outcome.labels l
                    WHERE l.revid = p.revid AND l.label))      AS label
  FROM register.predictions p
  JOIN landing.rc_events e ON e.revid = p.revid
  JOIN observed o          ON o.revid = p.revid
  LEFT JOIN landing.editor_state s ON s.user_key = e.user_name
  LEFT JOIN outcome.liftwing_scores lw ON lw.revid = p.revid
 WHERE p.role = 'champion'
   AND (NOT %(cohort_only)s OR e.in_maturity_cohort)
   -- Two conditions, and dropping either one biases the sample.
   --
   -- The first is elapsed time since the EDIT, applied to both classes alike.
   -- Without it a positive enters the moment it is found while a negative
   -- waits out the window, so at any instant the gradeable set is mostly
   -- reverts — measured at 178 of 178 on the first production run.
   --
   -- The second is that the outcome is actually determined: observed at or
   -- beyond the window, or already known reverted. Requiring the observation
   -- arm alone would be worse than the bias it fixes, because the labeller
   -- stops checking an edit once it is labelled positive, so a revert found at
   -- one hour never reaches a later checkpoint and would be excluded forever.
   AND EXTRACT(epoch FROM now() - p.event_ts) >= %(maturity)s
   AND (o.ever_positive OR o.last_observed_age >= %(maturity)s)
   AND p.scored_at >= %(window_start)s
 ORDER BY p.event_ts, p.revid
"""

# Everything matured in the window, including what the metric excludes. The
# denominator for the exclusion counts, so a reader can see what was dropped
# rather than only what survived.
EXCLUSIONS_SQL = """
WITH observed AS (
    SELECT c.revid, max(c.age_seconds) AS last_observed_age,
           bool_or(c.had_reverted_tag) AS ever_positive
      FROM outcome.label_checks c GROUP BY c.revid
)
SELECT count(*) FILTER (WHERE NOT matured)                        AS immature,
       count(*) FILTER (WHERE matured AND scored_late)            AS late,
       count(*) FILTER (WHERE matured AND scored_late AND label)  AS late_positive
  FROM (
    SELECT p.outcome_observable_at_scoring AS scored_late,
           (EXTRACT(epoch FROM now() - p.event_ts) >= %(maturity)s
            AND (o.ever_positive OR o.last_observed_age >= %(maturity)s)) AS matured,
           (o.ever_positive
            OR EXISTS (SELECT 1 FROM outcome.labels l
                        WHERE l.revid = p.revid AND l.label))       AS label
      FROM register.predictions p
      JOIN landing.rc_events e ON e.revid = p.revid
      LEFT JOIN observed o ON o.revid = p.revid
     WHERE p.role = 'champion' AND p.scored_at >= %(window_start)s
       AND (NOT %(cohort_only)s OR e.in_maturity_cohort)
  ) AS scoped
"""

INSERT_METRIC_SQL = """
INSERT INTO outcome.prediction_metrics
    (population, window_label, window_start, window_end, segment, segment_level, maturity_hours,
     provisional, n, n_positives, base_rate, weighted_base_rate, pr_auc,
     pr_auc_ci_low, pr_auc_ci_high, roc_auc, brier, baseline_pr_auc, margin,
     margin_ci_low, margin_ci_high, excluded_immature, excluded_late,
     excluded_late_base_rate, liftwing_n, liftwing_pr_auc, liftwing_margin,
     liftwing_margin_ci_low, liftwing_margin_ci_high, model_pr_auc_on_paired,
     code_commit, run_id)
VALUES (%(population)s, %(window_label)s, %(window_start)s, %(window_end)s, %(segment)s,
        %(segment_level)s, %(maturity_hours)s, %(provisional)s, %(n)s,
        %(n_positives)s, %(base_rate)s, %(weighted_base_rate)s, %(pr_auc)s,
        %(pr_auc_ci_low)s, %(pr_auc_ci_high)s, %(roc_auc)s, %(brier)s,
        %(baseline_pr_auc)s, %(margin)s, %(margin_ci_low)s, %(margin_ci_high)s,
        %(excluded_immature)s, %(excluded_late)s, %(excluded_late_base_rate)s,
        %(liftwing_n)s, %(liftwing_pr_auc)s, %(liftwing_margin)s,
        %(liftwing_margin_ci_low)s, %(liftwing_margin_ci_high)s,
        %(model_pr_auc_on_paired)s, %(commit)s, %(run_id)s)
RETURNING metric_id
"""

INSERT_BIN_SQL = """
INSERT INTO outcome.calibration_bins
    (metric_id, bin_index, bin_low, bin_high, n, mean_predicted, observed_rate,
     weighted_observed_rate)
VALUES (%(metric_id)s, %(bin_index)s, %(bin_low)s, %(bin_high)s, %(n)s,
        %(mean_predicted)s, %(observed_rate)s, %(weighted_observed_rate)s)
"""


def _weighted_rate(labels: np.ndarray, weights: np.ndarray) -> float | None:
    """The population rate, not the sample's.

    The frame keeps 50% of logged-out edits and 3% of registered ones, so these
    differ by roughly a factor of four. Publishing only the raw figure would
    describe a population that does not exist.
    """
    total = float(weights.sum())
    return float((labels * weights).sum() / total) if total else None


def bootstrap_pr_auc(
    y: np.ndarray, scores: np.ndarray, *, resamples: int = BOOTSTRAP_RESAMPLES
) -> tuple[float, float]:
    """A 95% interval for PR-AUC, resampling events.

    Published always, because early cohorts are small enough that a point
    estimate on its own invites a conclusion the data does not support
    (M4-FR-1).
    """
    rng = np.random.default_rng(SEED)
    n = len(y)
    draws = np.empty(resamples)
    for i in range(resamples):
        idx = rng.integers(0, n, n)
        draws[i] = np.nan if y[idx].sum() == 0 else average_precision_score(y[idx], scores[idx])
    return float(np.nanpercentile(draws, 2.5)), float(np.nanpercentile(draws, 97.5))


def paired_margin(
    y: np.ndarray, model: np.ndarray, baseline: np.ndarray, *, resamples: int = BOOTSTRAP_RESAMPLES
) -> tuple[float, float, float]:
    """Model minus the logged-out heuristic, on the same events, paired.

    The same opponent KC-2 was decided against, so the live number and the
    backtest number are answering the same question about different
    populations.
    """
    rng = np.random.default_rng(SEED)
    n = len(y)
    diffs = np.empty(resamples)
    for i in range(resamples):
        idx = rng.integers(0, n, n)
        if y[idx].sum() == 0:
            diffs[i] = np.nan
            continue
        diffs[i] = average_precision_score(y[idx], model[idx]) - average_precision_score(
            y[idx], baseline[idx]
        )
    observed = average_precision_score(y, model) - average_precision_score(y, baseline)
    return (
        float(observed),
        float(np.nanpercentile(diffs, 2.5)),
        float(np.nanpercentile(diffs, 97.5)),
    )


def compute(
    rows: list[dict[str, Any]],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    margin_interval: bool = True,
) -> dict[str, Any]:
    """Metrics for one set of matured, non-excluded predictions.

    Returns Nones rather than raising when the set cannot support a number —
    a window with no positives has no PR-AUC, and inventing one would be worse
    than publishing the gap with its `n` beside it.
    """
    n = len(rows)
    if n == 0:
        return {"n": 0, "n_positives": 0}

    y = np.asarray([1 if r["label"] else 0 for r in rows], dtype=int)
    scores = np.asarray([float(r["score"]) for r in rows], dtype=float)
    weights = np.asarray([float(r["sampling_weight"] or 1.0) for r in rows], dtype=float)
    baseline = np.asarray([1.0 if r["is_logged_out"] else 0.0 for r in rows], dtype=float)

    out: dict[str, Any] = {
        "n": n,
        "n_positives": int(y.sum()),
        "base_rate": float(y.mean()),
        "weighted_base_rate": _weighted_rate(y, weights),
        "pr_auc": None,
        "pr_auc_ci_low": None,
        "pr_auc_ci_high": None,
        "roc_auc": None,
        "brier": round(float(brier_score_loss(y, scores)), 6),
        "baseline_pr_auc": None,
        "margin": None,
        "margin_ci_low": None,
        "margin_ci_high": None,
    }

    if y.sum() == 0 or y.sum() == n:
        # One class. Every ranking metric is undefined, and the honest output is
        # the count rather than a number that happens to compute.
        return out

    out["pr_auc"] = round(float(average_precision_score(y, scores)), 6)
    low, high = bootstrap_pr_auc(y, scores, resamples=resamples)
    out["pr_auc_ci_low"], out["pr_auc_ci_high"] = round(low, 6), round(high, 6)
    out["roc_auc"] = round(float(roc_auc_score(y, scores)), 6)
    out["baseline_pr_auc"] = round(float(average_precision_score(y, baseline)), 6)

    if margin_interval:
        margin, m_low, m_high = paired_margin(y, scores, baseline, resamples=resamples)
        out["margin_ci_low"], out["margin_ci_high"] = round(m_low, 6), round(m_high, 6)
    else:
        margin = float(average_precision_score(y, scores)) - out["baseline_pr_auc"]
    out["margin"] = round(margin, 6)
    return out


def liftwing_comparison(
    rows: list[dict[str, Any]], *, resamples: int = BOOTSTRAP_RESAMPLES
) -> dict[str, Any]:
    """Bellwether against Wikimedia's production model, paired (M4-FR-18).

    On the subset both scored, and nothing else. Lift Wing is sampled rather
    than exhaustive, so setting its PR-AUC over a few hundred events against
    this project's over the whole window would be two populations dressed as a
    margin. `model_pr_auc_on_paired` exists for exactly that reason: it is the
    number the margin is actually built from, and it is not `pr_auc`.

    Positive margin means Bellwether ahead. SRS 6.4 predicted, before any model
    existed, that it would not be — and that prediction is left standing
    whichever way this falls.
    """
    paired = [r for r in rows if r.get("liftwing_score") is not None]
    empty: dict[str, Any] = {
        "liftwing_n": len(paired),
        "liftwing_pr_auc": None,
        "liftwing_margin": None,
        "liftwing_margin_ci_low": None,
        "liftwing_margin_ci_high": None,
        "model_pr_auc_on_paired": None,
    }
    # Too few to say anything. Published as a count with no margin, rather than
    # as a margin nobody should read.
    if len(paired) < 30:
        return empty

    y = np.asarray([1 if r["label"] else 0 for r in paired], dtype=int)
    if y.sum() == 0 or y.sum() == len(y):
        return empty

    ours = np.asarray([float(r["score"]) for r in paired], dtype=float)
    theirs = np.asarray([float(r["liftwing_score"]) for r in paired], dtype=float)
    margin, low, high = paired_margin(y, ours, theirs, resamples=resamples)
    return {
        "liftwing_n": len(paired),
        "liftwing_pr_auc": round(float(average_precision_score(y, theirs)), 6),
        "liftwing_margin": round(margin, 6),
        "liftwing_margin_ci_low": round(low, 6),
        "liftwing_margin_ci_high": round(high, 6),
        "model_pr_auc_on_paired": round(float(average_precision_score(y, ours)), 6),
    }


def calibration(
    rows: list[dict[str, Any]], *, bins: int = CALIBRATION_BINS
) -> list[dict[str, Any]]:
    """Reliability over equal-width score bins, raw and population-weighted.

    Equal-width rather than equal-count: the question is whether a score of 0.9
    means what it says, and quantile bins would move the boundaries every run so
    two runs could not be compared.
    """
    edges = np.linspace(0.0, 1.0, bins + 1)
    out = []
    for i in range(bins):
        low, high = edges[i], edges[i + 1]
        inside = [
            r
            for r in rows
            if low <= float(r["score"]) < high or (i == bins - 1 and float(r["score"]) == 1.0)
        ]
        if not inside:
            out.append(
                {
                    "bin_index": i,
                    "bin_low": float(low),
                    "bin_high": float(high),
                    "n": 0,
                    "mean_predicted": None,
                    "observed_rate": None,
                    "weighted_observed_rate": None,
                }
            )
            continue
        y = np.asarray([1 if r["label"] else 0 for r in inside], dtype=int)
        w = np.asarray([float(r["sampling_weight"] or 1.0) for r in inside], dtype=float)
        scores = np.asarray([float(r["score"]) for r in inside], dtype=float)
        out.append(
            {
                "bin_index": i,
                "bin_low": float(low),
                "bin_high": float(high),
                "n": len(inside),
                "mean_predicted": round(float(scores.mean()), 6),
                "observed_rate": round(float(y.mean()), 6),
                "weighted_observed_rate": round(_weighted_rate(y, w) or 0.0, 6),
            }
        )
    return out


def run(*, maturity: dict[str, int] | None = None) -> dict[str, Any]:
    run_id = new_run_id()
    settings = get_settings()
    now = datetime.now(UTC)
    written = 0
    summary: dict[str, Any] = {}

    with connect() as lock_conn, advisory_lock(lock_conn, METRICS_LOCK_KEY) as acquired:
        if not acquired:
            print("metrics: another run holds the lock, exiting cleanly")
            return {"skipped": True}

        with RunContext(run_id, job=JOB, window_to=now) as ctx, connect() as conn:
            # Before any work, so a database behind the code says so rather than
            # failing on a column name several queries in.
            require_current(conn)

            for population, maturity_seconds in (maturity or POPULATIONS).items():
                cohort_only = population == "maturity_cohort"
                for label, days in WINDOWS.items():
                    start = now - timedelta(days=days) if days else datetime(2000, 1, 1, tzinfo=UTC)

                    with conn.cursor() as cur:
                        scope = {
                            "maturity": maturity_seconds,
                            "window_start": start,
                            "cohort_only": cohort_only,
                        }
                        cur.execute(MATURED_SQL, scope)
                        matured = cur.fetchall()
                        cur.execute(EXCLUSIONS_SQL, scope)
                        excl = cur.fetchone() or {}

                    # M3-FR-10. Correct, and not neutral — see the module docstring.
                    usable = [r for r in matured if not r["scored_late"]]
                    late = int(excl.get("late") or 0)
                    late_positive = int(excl.get("late_positive") or 0)

                    common = {
                        "population": population,
                        "window_label": label,
                        "window_start": start if days else None,
                        "window_end": now,
                        "maturity_hours": maturity_seconds // 3600,
                        "provisional": True,
                        "excluded_immature": int(excl.get("immature") or 0),
                        "excluded_late": late,
                        "excluded_late_base_rate": (late_positive / late) if late else None,
                        "commit": settings.build_id,
                        "run_id": run_id,
                    }

                    metric_id = _write(
                        conn,
                        {**common, "segment": "all", "segment_level": "all"},
                        usable,
                        aggregate=True,
                    )
                    written += 1
                    if label == "7d":
                        summary[population] = {
                            "n": len(usable),
                            "positives": sum(1 for r in usable if r["label"]),
                            "excluded_late": late,
                        }

                    # The aggregate carries the calibration curve; segments do not,
                    # because a reliability bin split four ways holds nothing.
                    if metric_id is not None:
                        _write_bins(conn, metric_id, usable)

                    for segment, column in SEGMENTS.items():
                        for level in sorted({str(r[column]) for r in usable}):
                            subset = [r for r in usable if str(r[column]) == level]
                            _write(
                                conn,
                                {**common, "segment": segment, "segment_level": level},
                                subset,
                            )
                            written += 1

                ctx.rows_written = written

    print(f"metrics: wrote {written} rows")
    for population, seen in summary.items():
        hours = (maturity or POPULATIONS)[population] // 3600
        if seen["n"]:
            print(
                f"  7d {population:<16} n={seen['n']:,}  positives={seen['positives']:,}  "
                f"(matured at {hours}h)"
            )
        else:
            print(
                f"  7d {population:<16} nothing gradeable yet — no scored event is "
                f"{hours}h old with its outcome determined"
            )
    return {"rows": written, "seven_day": summary}


def _write(
    conn: Any, common: dict[str, Any], rows: list[dict[str, Any]], *, aggregate: bool = False
) -> int | None:
    payload = {
        **common,
        **(liftwing_comparison(rows) if aggregate else {}),
        **compute(
            rows,
            resamples=BOOTSTRAP_RESAMPLES if aggregate else SEGMENT_RESAMPLES,
            margin_interval=aggregate,
        ),
    }
    payload.setdefault("base_rate", None)
    payload.setdefault("liftwing_n", 0)
    for key in (
        "liftwing_n",
        "liftwing_pr_auc",
        "liftwing_margin",
        "liftwing_margin_ci_low",
        "liftwing_margin_ci_high",
        "model_pr_auc_on_paired",
        "weighted_base_rate",
        "pr_auc",
        "pr_auc_ci_low",
        "pr_auc_ci_high",
        "roc_auc",
        "brier",
        "baseline_pr_auc",
        "margin",
        "margin_ci_low",
        "margin_ci_high",
    ):
        payload.setdefault(key, None)
    with conn.cursor() as cur:
        cur.execute(INSERT_METRIC_SQL, payload)
        row = cur.fetchone()
    return int(row["metric_id"]) if row else None


def _write_bins(conn: Any, metric_id: int, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            INSERT_BIN_SQL,
            [{**b, "metric_id": metric_id} for b in calibration(rows)],
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--maturity-hours",
        type=int,
        default=PROVISIONAL_MATURITY_SECONDS // 3600,
        help="the window must be one the labeller actually observes at; see the module docstring",
    )
    parser.add_argument(
        "--cohort-maturity-hours",
        type=int,
        default=COHORT_MATURITY_SECONDS // 3600,
        help="the cohort receives the full checkpoint grid, so a shorter window is observable",
    )
    args = parser.parse_args()
    run(
        maturity={
            "all": args.maturity_hours * 3600,
            "maturity_cohort": args.cohort_maturity_hours * 3600,
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
