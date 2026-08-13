"""Baselines, a model, and KC-2 (M2 §4, C-6 to C-8).

KC-2: **if no leak-free feature set beats the logged-out heuristic by +0.05
PR-AUC, the project stops at M2.** That margin is fixed in the M2 spec, written
before any model existed, and this module does not get to reconsider it.

Three things about how the evaluation is set up, each of which could quietly
decide the answer:

**Maturity is provisional at 48 hours, and labelled as such.** The real window
needs the maturity cohort to age (M2 §2.1). The curve is flat from 12h to 48h
in the region where observations actually exist, so 48h is defensible — but it
is a placeholder, and every number here has to be re-run against the real
window before it means anything final.

**Negatives are derived from observation, not absence.** `outcome.labels` holds
almost only positives, because the labelling job writes a negative only at the
final checkpoint and nothing has reached it. So a negative here means *checked
at 48 hours or later and no revert seen*, never *no row found*. Treating
unchecked events as negatives would inflate every score, and the model would
learn to predict "has the labeller got to this yet".

**The evaluation window is the backfilled census, and that is a choice.** Those
events share one ingestion regime and one observation pattern, so their labels
are equally complete. Mixing them with live rows would mean the positive rate
differed by ingestion regime, and any feature correlated with recency would
start predicting label completeness instead of reverts. M2 §6 permits excluding
them; it forbids doing so silently, hence this paragraph and the printed
banner.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score

from bellwether import features, knowability, state
from bellwether.config import get_settings
from bellwether.db import connect
from bellwether.runlog import new_run_id

PROVISIONAL_MATURITY_SECONDS = 48 * 3600

# Fixed in Bellwether_M2_Spec.md §4 before any model existed.
KC2_MARGIN = 0.05
BOOTSTRAP_RESAMPLES = 2_000
SEED = 20260814

INSERT_EVALUATION_SQL = """
INSERT INTO outcome.evaluations
    (window_start, window_end, maturity_hours, provisional, n_events, n_positives,
     n_features, pr_auc, margin, ci_low, ci_high, margin_required, clears_kc2,
     feature_names, code_commit, run_id)
VALUES (%(window_start)s, %(window_end)s, %(maturity_hours)s, %(provisional)s,
        %(n_events)s, %(n_positives)s, %(n_features)s, %(pr_auc)s::jsonb, %(margin)s,
        %(ci_low)s, %(ci_high)s, %(margin_required)s, %(clears)s,
        %(features)s, %(commit)s, %(run_id)s)
"""

DATASET_SQL = """
WITH observed AS (
    SELECT c.revid,
           max(c.age_seconds)                                   AS last_observed_age,
           bool_or(c.had_reverted_tag)                          AS ever_positive
      FROM outcome.label_checks c
     GROUP BY c.revid
)
SELECT e.revid, e.old_revid, e.event_ts, e.ns, e.title, e.user_name, e.user_id,
       e.is_anon, e.is_temp, e.is_minor, e.is_bot, e.comment, e.comment_hidden,
       e.oldlen, e.newlen, e.tags, e.sampling_stratum, e.sampling_weight,
       (e.tags && ARRAY['mw-undo','mw-rollback','mw-manual-revert']) AS is_reverting,
       (SELECT min(r.revert_ts) FROM outcome.revert_events r
         WHERE r.reverted_revid = e.revid)                       AS reverted_at,
       (o.ever_positive
        OR EXISTS (SELECT 1 FROM outcome.labels l
                    WHERE l.revid = e.revid AND l.label))        AS label
  FROM landing.rc_events e
  JOIN observed o ON o.revid = e.revid
 -- Matured, and matured by OBSERVATION: either seen at or beyond the window,
 -- or already known reverted. An event nobody has looked at is not a negative.
 WHERE (o.last_observed_age >= %(maturity)s OR o.ever_positive)
   AND e.event_ts <  %(window_end)s
   AND e.event_ts >= %(window_start)s
 ORDER BY e.event_ts, e.revid
"""


def load(conn: Any, *, window_start: Any, window_end: Any) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            DATASET_SQL,
            {
                "maturity": PROVISIONAL_MATURITY_SECONDS,
                "window_start": window_start,
                "window_end": window_end,
            },
        )
        return cur.fetchall()


def build_matrix(rows: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Features and labels, in time order, with history folded in as we go.

    The same read-emit-fold ordering the replay uses. Building the matrix any
    other way would let an event into its own history and the numbers below
    would be unreproducible in production.
    """
    knowability.run_all()

    names = features.feature_names()
    st: dict[str, Any] = {}
    pending: list[tuple[Any, dict[str, Any]]] = []
    matrix, labels = [], []

    for row in rows:
        now = row["event_ts"]
        still = []
        for revert_ts, reverted in pending:
            if revert_ts <= now:
                state.observe_revert(st, reverted)
            else:
                still.append((revert_ts, reverted))
        pending = still

        vector = features.build(row, state.history_for(st, row))
        matrix.append([vector[n] for n in names])
        labels.append(1 if row["label"] else 0)

        state.observe(st, row)
        if row["reverted_at"] is not None:
            pending.append((row["reverted_at"], row))

    return np.asarray(matrix, dtype=float), np.asarray(labels, dtype=int), names


def baseline_scores(rows: list[dict[str, Any]]) -> dict[str, np.ndarray]:
    """The three baselines from M2 §4. No model, no fitting."""
    n = len(rows)
    return {
        # No model at all: the order a patroller would work through.
        "arrival_order": np.arange(n, 0, -1, dtype=float),
        # The opponent that matters. One boolean already on every event.
        "logged_out": np.asarray([1.0 if (r["is_anon"] or r["is_temp"]) else 0.0 for r in rows]),
        "abs_byte_delta": np.asarray(
            [abs((r["newlen"] or 0) - (r["oldlen"] or 0)) for r in rows], dtype=float
        ),
    }


def rolling_origin_scores(
    matrix: np.ndarray, labels: np.ndarray, *, folds: int = 4
) -> tuple[np.ndarray, np.ndarray]:
    """Train on the past, score the future, fold by fold (M2-FR-15).

    Never a random split. The data is temporally ordered and a random split
    lets the model see the future of the very editors and pages it is scoring —
    the same leak the knowability guard exists to prevent, reintroduced by the
    evaluation rather than the features.
    """
    n = len(labels)
    edges = [int(n * (i + 1) / (folds + 1)) for i in range(folds + 1)]
    scores = np.full(n, np.nan)

    for i in range(folds):
        train_end, test_end = edges[i], edges[i + 1]
        y_train = labels[:train_end]
        if len(set(y_train.tolist())) < 2:
            continue
        model = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.1, random_state=SEED)
        model.fit(matrix[:train_end], y_train)
        scores[train_end:test_end] = model.predict_proba(matrix[train_end:test_end])[:, 1]

    scored = ~np.isnan(scores)
    return scores[scored], scored


def paired_bootstrap(
    y: np.ndarray, a: np.ndarray, b: np.ndarray, *, resamples: int = BOOTSTRAP_RESAMPLES
) -> tuple[float, float, float]:
    """Difference in PR-AUC, with a 95% interval, resampling EVENTS.

    Paired: both scorers keep their scores for the same resampled events.
    Resampling them independently would break the pairing and inflate the
    interval, so a genuinely better model would fail to clear the margin and
    the failure would look like the model's fault.
    """
    rng = np.random.default_rng(SEED)
    n = len(y)
    diffs = np.empty(resamples)
    for i in range(resamples):
        idx = rng.integers(0, n, n)
        if y[idx].sum() == 0:
            diffs[i] = np.nan
            continue
        diffs[i] = average_precision_score(y[idx], a[idx]) - average_precision_score(y[idx], b[idx])
    observed = average_precision_score(y, a) - average_precision_score(y, b)
    return observed, float(np.nanpercentile(diffs, 2.5)), float(np.nanpercentile(diffs, 97.5))


def parse_when(text: str) -> datetime:
    """ISO 8601 to an aware datetime.

    The workflow passes strings. A str parameter reaching a timestamptz column
    is sent as text, and Postgres has no implicit assignment cast for it — a
    failure that surfaces at the very end of a run, after all the work.
    """
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def run(*, window_start: str, window_end: str, folds: int = 4) -> dict[str, Any]:
    run_id = new_run_id()
    start, end = parse_when(window_start), parse_when(window_end)
    with connect() as conn:
        rows = load(conn, window_start=start, window_end=end)

    print("=" * 74)
    print("PROVISIONAL. Maturity fixed at 48h because the real window needs the")
    print("maturity cohort to age (M2 §2.1). Evaluation window is the backfilled")
    print("census only — one ingestion regime, so labels are equally complete.")
    print("Every number here must be re-run against the real window.")
    print("=" * 74)

    if len(rows) < 500:
        print(f"\nOnly {len(rows)} matured observed events. Too few to decide KC-2.")
        return {"decided": False, "n": len(rows)}

    matrix, labels, names = build_matrix(rows)
    print(
        f"\nevents {len(rows):,}   positives {int(labels.sum()):,} "
        f"({100 * labels.mean():.2f}%)   features {len(names)}"
    )

    model_scores, scored = rolling_origin_scores(matrix, labels, folds=folds)
    y = labels[scored]
    base = {k: v[scored] for k, v in baseline_scores(rows).items()}

    print(f"scored out of sample: {len(y):,}   positives {int(y.sum()):,}\n")
    print(f"{'scorer':<20}{'PR-AUC':>10}")
    results = {name: average_precision_score(y, s) for name, s in base.items()}
    results["model"] = average_precision_score(y, model_scores)
    for name, value in sorted(results.items(), key=lambda kv: -kv[1]):
        print(f"{name:<20}{value:>10.4f}")

    observed, lo, hi = paired_bootstrap(y, model_scores, base["logged_out"])
    print(f"\nmodel minus logged-out heuristic: {observed:+.4f}  95% CI [{lo:+.4f}, {hi:+.4f}]")
    print(f"KC-2 margin required:             {KC2_MARGIN:+.4f}")

    clears = observed >= KC2_MARGIN and lo > 0
    print(f"\nKC-2: {'CLEARED' if clears else 'NOT CLEARED'} (provisional)")
    if not clears:
        print("On the real maturity window this would end the project at M2.")

    record = {
        "window_start": start,
        "window_end": end,
        "maturity_hours": PROVISIONAL_MATURITY_SECONDS // 3600,
        "provisional": True,
        "n_events": len(y),
        "n_positives": int(y.sum()),
        "n_features": len(names),
        "pr_auc": json.dumps({k: round(v, 6) for k, v in results.items()}),
        "margin": observed,
        "ci_low": lo,
        "ci_high": hi,
        "margin_required": KC2_MARGIN,
        "clears": clears,
        "features": names,
        "commit": get_settings().build_id,
        "run_id": run_id,
    }
    with connect() as conn, conn.cursor() as cur:
        cur.execute(INSERT_EVALUATION_SQL, record)
    print("\nrecorded to outcome.evaluations - served at /kc2")

    return {
        "decided": True,
        "n": len(y),
        "positives": int(y.sum()),
        "pr_auc": results,
        "margin": observed,
        "ci": [lo, hi],
        "clears": clears,
        "provisional": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", default="2026-08-10T00:00:00Z")
    parser.add_argument("--to", dest="end", default="2026-08-11T00:00:00Z")
    parser.add_argument("--folds", type=int, default=4)
    args = parser.parse_args()
    run(window_start=args.start, window_end=args.end, folds=args.folds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
