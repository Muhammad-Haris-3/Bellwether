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

JOB = "metrics"
METRICS_LOCK_KEY = 815_010

# The M2 placeholder, carried here rather than re-decided. The real window needs
# the maturity cohort to age (M2 C-1/C-2), and every row records which one it
# used so no number is ever read without it.
PROVISIONAL_MATURITY_SECONDS = 48 * 3600

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
       (o.ever_positive
        OR EXISTS (SELECT 1 FROM outcome.labels l
                    WHERE l.revid = p.revid AND l.label))      AS label
  FROM register.predictions p
  JOIN landing.rc_events e ON e.revid = p.revid
  JOIN observed o          ON o.revid = p.revid
  LEFT JOIN landing.editor_state s ON s.user_key = e.user_name
 WHERE p.role = 'champion'
   -- Matured, and matured by OBSERVATION. Not "old enough": an event nobody
   -- has looked at is not a negative, and this is the filter M4-FR-5 requires
   -- to live in the query rather than in whoever calls it.
   AND (o.last_observed_age >= %(maturity)s OR o.ever_positive)
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
           (o.last_observed_age >= %(maturity)s OR o.ever_positive) AS matured,
           (o.ever_positive
            OR EXISTS (SELECT 1 FROM outcome.labels l
                        WHERE l.revid = p.revid AND l.label))       AS label
      FROM register.predictions p
      LEFT JOIN observed o ON o.revid = p.revid
     WHERE p.role = 'champion' AND p.scored_at >= %(window_start)s
  ) AS scoped
"""

INSERT_METRIC_SQL = """
INSERT INTO outcome.prediction_metrics
    (window_label, window_start, window_end, segment, segment_level, maturity_hours,
     provisional, n, n_positives, base_rate, weighted_base_rate, pr_auc,
     pr_auc_ci_low, pr_auc_ci_high, roc_auc, brier, baseline_pr_auc, margin,
     margin_ci_low, margin_ci_high, excluded_immature, excluded_late,
     excluded_late_base_rate, code_commit, run_id)
VALUES (%(window_label)s, %(window_start)s, %(window_end)s, %(segment)s,
        %(segment_level)s, %(maturity_hours)s, %(provisional)s, %(n)s,
        %(n_positives)s, %(base_rate)s, %(weighted_base_rate)s, %(pr_auc)s,
        %(pr_auc_ci_low)s, %(pr_auc_ci_high)s, %(roc_auc)s, %(brier)s,
        %(baseline_pr_auc)s, %(margin)s, %(margin_ci_low)s, %(margin_ci_high)s,
        %(excluded_immature)s, %(excluded_late)s, %(excluded_late_base_rate)s,
        %(commit)s, %(run_id)s)
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


def run(*, maturity_seconds: int = PROVISIONAL_MATURITY_SECONDS) -> dict[str, Any]:
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
            for label, days in WINDOWS.items():
                start = now - timedelta(days=days) if days else datetime(2000, 1, 1, tzinfo=UTC)

                with conn.cursor() as cur:
                    cur.execute(MATURED_SQL, {"maturity": maturity_seconds, "window_start": start})
                    matured = cur.fetchall()
                    cur.execute(
                        EXCLUSIONS_SQL, {"maturity": maturity_seconds, "window_start": start}
                    )
                    excl = cur.fetchone() or {}

                # M3-FR-10. Correct, and not neutral — see the module docstring.
                usable = [r for r in matured if not r["scored_late"]]
                late = int(excl.get("late") or 0)
                late_positive = int(excl.get("late_positive") or 0)

                common = {
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
                    summary = {**compute(usable), "excluded_late": late}

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

    print(f"metrics: wrote {written} rows over {len(WINDOWS)} windows")
    if summary.get("n"):
        print(
            f"  7d  n={summary['n']:,}  positives={summary['n_positives']:,}  "
            f"PR-AUC {summary.get('pr_auc')}  "
            f"CI [{summary.get('pr_auc_ci_low')}, {summary.get('pr_auc_ci_high')}]"
        )
        print(f"      margin vs logged-out {summary.get('margin')}")
    else:
        print("  7d  nothing matured yet — the register is younger than the maturity window")
    return {"rows": written, "seven_day": summary}


def _write(
    conn: Any, common: dict[str, Any], rows: list[dict[str, Any]], *, aggregate: bool = False
) -> int | None:
    payload = {
        **common,
        **compute(
            rows,
            resamples=BOOTSTRAP_RESAMPLES if aggregate else SEGMENT_RESAMPLES,
            margin_interval=aggregate,
        ),
    }
    payload.setdefault("base_rate", None)
    for key in (
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
    parser.add_argument("--maturity-hours", type=int, default=48)
    args = parser.parse_args()
    run(maturity_seconds=args.maturity_hours * 3600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
