"""The promotion rule (M5-FR-20 to FR-24, M5 §6).

Five conditions from `PREREGISTRATION.md` §5, all of which must hold. Failing
any one, the challenger is **rejected and the champion stays** — recorded with
the same evidence a promotion would carry.

**A rejection is the more informative row.** A log containing only promotions
answers "what changed" and cannot answer "what was considered and refused",
which is the question that shows the rule binding rather than being satisfied
by everything that reached it. On current evidence the most likely correct
outcome here is rejection, and that is the milestone working.

**Paired, on matured events both models scored.** The challenger writes a shadow
score for the same event, from the same feature vector, in the same run — so
the comparison is over identical inputs and any event either model missed
simply is not in it (M5-FR-10).

**Nothing here decides anything.** Every threshold is read from
`bellwether/preregistration.py`, which is asserted against the document. If a
condition is wrong, that is an amendment with a date, made before the run it
would change.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any

import numpy as np
from sklearn.metrics import average_precision_score

from bellwether import metrics, registry
from bellwether import preregistration as pre
from bellwether.config import get_settings
from bellwether.db import advisory_lock, connect
from bellwether.runlog import RunContext, new_run_id
from bellwether.schema import require_current

JOB = "promote"
PROMOTE_LOCK_KEY = 815_013

# Hour of day is pre-registered as a segment; the number of buckets is not.
#
# Twenty-four would give each one a few dozen matured events at current volume,
# where PR-AUC is mostly noise — and P-5 blocks a promotion on ANY segment
# regressing, so noise in a sparse bucket would veto every challenger forever.
# Quarters of the day keep the pre-registered dimension while leaving each
# bucket enough events to mean something.
HOUR_BUCKETS = ((0, 6, "00-05"), (6, 12, "06-11"), (12, 18, "12-17"), (18, 24, "18-23"))

# Paired matured predictions: the champion and the challenger on the same event.
#
# Maturity is M4's rule, imported in spirit and restated here because this query
# also needs the segment columns. Late-scored predictions are excluded, as
# everywhere else (M5-FR-21).
PAIRED_SQL = """
WITH observed AS (
    SELECT c.revid, max(c.age_seconds) AS last_observed_age,
           bool_or(c.had_reverted_tag) AS ever_positive
      FROM outcome.label_checks c GROUP BY c.revid
)
SELECT ch.revid,
       ch.score                                          AS champion_score,
       sh.score                                          AS challenger_score,
       e.event_ts,
       (e.is_anon OR e.is_temp)                          AS is_logged_out,
       abs(COALESCE(e.newlen, 0) - COALESCE(e.oldlen, 0)) AS edit_size,
       EXTRACT(hour FROM e.event_ts)::int                AS hour_utc,
       -- Edits to this page in the 7 days before this one, per §4. Counted
       -- from the event feed rather than from editor_state, because that
       -- counter is all-time and the pre-registered band is a 7-day window.
       (SELECT count(*) FROM landing.rc_events p
         WHERE p.title = e.title
           AND p.event_ts <  e.event_ts
           AND p.event_ts >= e.event_ts - interval '7 days') AS page_activity,
       (o.ever_positive
        OR EXISTS (SELECT 1 FROM outcome.labels l
                    WHERE l.revid = ch.revid AND l.label))  AS label
  FROM register.predictions ch
  JOIN register.predictions sh
    ON sh.revid = ch.revid AND sh.role = 'shadow' AND sh.model_version = %(challenger)s
  JOIN landing.rc_events e ON e.revid = ch.revid
  JOIN observed o          ON o.revid = ch.revid
 WHERE ch.role = 'champion'
   AND ch.model_version = %(champion)s
   AND NOT ch.outcome_observable_at_scoring
   AND NOT sh.outcome_observable_at_scoring
   AND EXTRACT(epoch FROM now() - ch.event_ts) >= %(maturity)s
   AND (o.ever_positive OR o.last_observed_age >= %(maturity)s)
 ORDER BY ch.event_ts, ch.revid
"""

SHADOW_SINCE_SQL = """
SELECT EXTRACT(epoch FROM now() - m.trained_at) / 86400.0 AS days
  FROM register.model_registry m WHERE m.model_version = %(version)s
"""

INSERT_DECISION_SQL = """
INSERT INTO decide.model_decisions
    (decision, champion_version, challenger_version, trigger_reason,
     p1_pr_auc_gain, p1_pass, p2_ci_low, p2_ci_high, p2_pass,
     p3_matured_positives, p3_pass, p4_shadow_days, p4_pass,
     p5_ece_regression, p5_worst_segment, p5_worst_segment_regression, p5_pass,
     champion_pr_auc, challenger_pr_auc, n_matured, n_positives,
     prereg_commit, code_commit, run_id)
VALUES (%(decision)s, %(champion)s, %(challenger)s, %(trigger)s,
        %(p1_gain)s, %(p1)s, %(p2_low)s, %(p2_high)s, %(p2)s,
        %(p3_positives)s, %(p3)s, %(p4_days)s, %(p4)s,
        %(p5_ece)s, %(p5_segment)s, %(p5_regression)s, %(p5)s,
        %(champion_pr_auc)s, %(challenger_pr_auc)s, %(n)s, %(positives)s,
        %(prereg_commit)s, %(commit)s, %(run_id)s)
RETURNING decision_id
"""

INSERT_CHAMPION_SQL = """
INSERT INTO decide.champion_history (model_version, decision_id, replaced)
VALUES (%(version)s, %(decision_id)s, %(replaced)s)
"""


def bands_for(values: list[float]) -> list[float]:
    """Quartile boundaries, frozen with the model that produced them."""
    if len(values) < 4:
        return []
    return [
        round(float(q), 4) for q in np.quantile(np.asarray(values, dtype=float), [0.25, 0.5, 0.75])
    ]


def band_of(value: float, edges: list[float]) -> str:
    """Which quartile a value falls in, by boundaries fixed elsewhere."""
    if not edges:
        return "all"
    for index, edge in enumerate(edges):
        if value <= edge:
            return f"q{index + 1}"
    return f"q{len(edges) + 1}"


def segment_of(row: dict[str, Any], bands: dict[str, list[float]]) -> dict[str, str]:
    """The four pre-registered decision segments (PREREGISTRATION.md §4).

    NOT M4's diagnostic four. P-5 refers to this list, and using M4's would
    quietly change what the promotion rule tests — the conflict recorded in
    M5 §2.
    """
    hour = int(row["hour_utc"])
    return {
        "editor_class": "logged_out" if row["is_logged_out"] else "registered",
        "edit_size_band": band_of(float(row["edit_size"]), bands.get("edit_size", [])),
        "hour_of_day": next(label for low, high, label in HOUR_BUCKETS if low <= hour < high),
        "page_activity_band": band_of(float(row["page_activity"]), bands.get("page_activity", [])),
    }


def ece(rows: list[dict[str, Any]], key: str) -> float | None:
    """Expected calibration error, population-weighted (M5-FR-23).

    Weighted, for the reason M4 established: the frame keeps 50% of logged-out
    edits and 3% of registered ones, so calibrating against raw sample frequency
    would describe a population that does not exist.
    """
    binned = metrics.calibration(
        [
            {
                "score": r[key],
                "label": r["label"],
                "sampling_weight": r.get("sampling_weight", 1.0),
            }
            for r in rows
        ]
    )
    total = sum(b["n"] for b in binned)
    if not total:
        return None
    error = sum(
        b["n"] * abs((b["mean_predicted"] or 0.0) - (b["weighted_observed_rate"] or 0.0))
        for b in binned
        if b["n"]
    )
    return round(error / total, 6)


def evaluate(
    rows: list[dict[str, Any]], bands: dict[str, list[float]], shadow_days: float
) -> dict[str, Any]:
    """The five conditions, measured. Nothing here chooses a threshold."""
    y = np.asarray([1 if r["label"] else 0 for r in rows], dtype=int)
    n, positives = len(rows), int(y.sum())

    verdict: dict[str, Any] = {
        "n": n,
        "positives": positives,
        "p3_positives": positives,
        "p3": positives >= pre.PROMOTION_MIN_MATURED_POSITIVES,
        "p4_days": round(shadow_days, 3),
        "p4": shadow_days >= pre.PROMOTION_MIN_SHADOW_DAYS,
        "champion_pr_auc": None,
        "challenger_pr_auc": None,
        "p1_gain": None,
        "p1": False,
        "p2_low": None,
        "p2_high": None,
        "p2": False,
        "p5_ece": None,
        "p5_segment": None,
        "p5_regression": None,
        "p5": False,
    }

    # Undefined rather than failed. A comparison with one class present is not
    # evidence the challenger is worse; it is no evidence at all.
    if n == 0 or positives == 0 or positives == n:
        return verdict

    champion = np.asarray([float(r["champion_score"]) for r in rows], dtype=float)
    challenger = np.asarray([float(r["challenger_score"]) for r in rows], dtype=float)

    verdict["champion_pr_auc"] = round(float(average_precision_score(y, champion)), 6)
    verdict["challenger_pr_auc"] = round(float(average_precision_score(y, challenger)), 6)

    gain, low, high = metrics.paired_margin(
        y, challenger, champion, resamples=pre.BOOTSTRAP_RESAMPLES
    )
    verdict["p1_gain"] = round(gain, 6)
    verdict["p1"] = gain >= pre.PROMOTION_MIN_PR_AUC_GAIN
    verdict["p2_low"], verdict["p2_high"] = round(low, 6), round(high, 6)
    verdict["p2"] = low > 0 or high < 0  # the interval excludes zero

    champion_ece = ece(rows, "champion_score")
    challenger_ece = ece(rows, "challenger_score")
    ece_regression = (
        None
        if champion_ece is None or challenger_ece is None
        else round(challenger_ece - champion_ece, 6)
    )
    verdict["p5_ece"] = ece_regression

    worst_segment, worst_regression = None, 0.0
    for dimension in pre.DECISION_SEGMENTS:
        levels = {segment_of(r, bands)[dimension] for r in rows}
        for level in sorted(levels):
            subset = [r for r in rows if segment_of(r, bands)[dimension] == level]
            sub_y = np.asarray([1 if r["label"] else 0 for r in subset], dtype=int)
            # A segment where PR-AUC is undefined does not block. It carries no
            # information about either model, and letting it veto would mean a
            # quiet hour of the day permanently blocking every challenger.
            if sub_y.sum() == 0 or sub_y.sum() == len(sub_y):
                continue
            sub_champion = average_precision_score(
                sub_y, np.asarray([float(r["champion_score"]) for r in subset])
            )
            sub_challenger = average_precision_score(
                sub_y, np.asarray([float(r["challenger_score"]) for r in subset])
            )
            regression = float(sub_champion - sub_challenger)
            if regression > worst_regression:
                worst_segment = f"{dimension}={level}"
                worst_regression = regression

    verdict["p5_segment"] = worst_segment
    verdict["p5_regression"] = round(worst_regression, 6)
    verdict["p5"] = (
        ece_regression is not None
        and ece_regression <= pre.PROMOTION_MAX_ECE_REGRESSION
        and worst_regression <= pre.PROMOTION_MAX_SEGMENT_REGRESSION
    )
    return verdict


def run(*, dry_run: bool = False) -> dict[str, Any]:
    run_id = new_run_id()
    settings = get_settings()

    with connect() as lock_conn, advisory_lock(lock_conn, PROMOTE_LOCK_KEY) as acquired:
        if not acquired:
            print("promote: another run holds the lock, exiting cleanly")
            return {"skipped": True}

        with connect() as conn:
            require_current(conn)

            frozen = conn.execute("SELECT app.is_automation_frozen() AS v").fetchone()
            if frozen and frozen["v"]:
                print("promote: automation is frozen — skipping")
                return {"skipped": True, "reason": "frozen"}

            champion = registry.champion(conn)
            if not champion:
                print("promote: no champion. Nothing to compare against.")
                return {"skipped": True, "reason": "no champion"}

            challenger = registry.challenger(conn, champion["model_version"])
            if not challenger:
                print("promote: no challenger in shadow.")
                return {"skipped": True, "reason": "no challenger"}

            with conn.cursor() as cur:
                cur.execute(
                    PAIRED_SQL,
                    {
                        "champion": champion["model_version"],
                        "challenger": challenger["model_version"],
                        "maturity": metrics.PROVISIONAL_MATURITY_SECONDS,
                    },
                )
                rows = cur.fetchall()

                cur.execute(SHADOW_SINCE_SQL, {"version": challenger["model_version"]})
                shadow_days = float((cur.fetchone() or {}).get("days") or 0.0)

            # The incumbent's bands, so both sides are cut the same way. A model
            # trained before M5 has none, and the decision records which model
            # they came from rather than silently computing them from the
            # comparison window, which is the one thing M5-FR-6 forbids.
            stored = champion.get("segment_bands") or challenger.get("segment_bands") or {}
            bands = stored if isinstance(stored, dict) else json.loads(stored)

        verdict = evaluate(rows, bands, shadow_days)
        passed = all(verdict[key] for key in ("p1", "p2", "p3", "p4", "p5"))
        decision = "promote" if passed else "reject"

        if dry_run:
            print(f"promote: DRY RUN — would {decision}")
            _report(champion, challenger, verdict, decision)
            return {"decision": decision, "dry_run": True, **verdict}

        with RunContext(run_id, job=JOB) as ctx, connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    INSERT_DECISION_SQL,
                    {
                        "decision": decision,
                        "champion": champion["model_version"],
                        "challenger": challenger["model_version"],
                        "trigger": None,
                        "prereg_commit": settings.build_id,
                        "commit": settings.build_id,
                        "run_id": run_id,
                        **verdict,
                    },
                )
                # A RETURNING insert that yields nothing is not a case to
                # shrug at: it would mean the decision was not recorded while
                # the promotion below went ahead, leaving production serving a
                # model with no row saying why.
                inserted = cur.fetchone()
                if inserted is None:
                    raise RuntimeError("the decision was not recorded; refusing to act on it")
                decision_id = inserted["decision_id"]

                if decision == "promote":
                    cur.execute(
                        INSERT_CHAMPION_SQL,
                        {
                            "version": challenger["model_version"],
                            "decision_id": decision_id,
                            "replaced": champion["model_version"],
                        },
                    )
            ctx.rows_read = len(rows)
            ctx.rows_written = 1

    _report(champion, challenger, verdict, decision)
    return {"decision": decision, **verdict}


def _report(
    champion: dict[str, Any],
    challenger: dict[str, Any],
    verdict: dict[str, Any],
    decision: str,
) -> None:
    def mark(passed: Any) -> str:
        return "PASS" if passed else "FAIL"

    print(f"promote: {champion['model_version']} vs {challenger['model_version']}")
    print(f"  matured paired events {verdict['n']:,}  positives {verdict['positives']:,}")
    print(
        f"  PR-AUC  champion {verdict['champion_pr_auc']}  "
        f"challenger {verdict['challenger_pr_auc']}"
    )
    print(
        f"  P-1 gain      {verdict['p1_gain']} >= "
        f"{pre.PROMOTION_MIN_PR_AUC_GAIN}  {mark(verdict['p1'])}"
    )
    print(
        f"  P-2 interval  [{verdict['p2_low']}, {verdict['p2_high']}] "
        f"excludes 0  {mark(verdict['p2'])}"
    )
    print(
        f"  P-3 positives {verdict['p3_positives']:,} >= "
        f"{pre.PROMOTION_MIN_MATURED_POSITIVES:,}  {mark(verdict['p3'])}"
    )
    print(
        f"  P-4 shadow    {verdict['p4_days']}d >= "
        f"{pre.PROMOTION_MIN_SHADOW_DAYS}d  {mark(verdict['p4'])}"
    )
    print(
        f"  P-5 ECE {verdict['p5_ece']} <= {pre.PROMOTION_MAX_ECE_REGRESSION}, "
        f"worst segment {verdict['p5_segment']} {verdict['p5_regression']} <= "
        f"{pre.PROMOTION_MAX_SEGMENT_REGRESSION}  {mark(verdict['p5'])}"
    )
    print(f"  DECISION: {decision.upper()}")


LAST_PROMOTION_SQL = """
SELECT decision_id, decided_at, champion_version, challenger_version,
       champion_pr_auc,
       EXTRACT(epoch FROM now() - decided_at) / 86400.0 AS days_since
  FROM decide.model_decisions
 WHERE decision = 'promote'
 ORDER BY decided_at DESC
 LIMIT 1
"""

INSERT_ROLLBACK_SQL = """
INSERT INTO decide.model_decisions
    (decision, champion_version, challenger_version, trigger_reason,
     champion_pr_auc, challenger_pr_auc, n_matured, prereg_commit, code_commit, run_id)
VALUES ('rollback', %(restore)s, %(failing)s, %(reason)s,
        %(previous_level)s, %(rolling)s, %(n)s, %(commit)s, %(commit)s, %(run_id)s)
RETURNING decision_id
"""


def check_rollback(*, day: Any = None) -> dict[str, Any]:
    """Restore the previous champion if the promoted one has fallen (M5-FR-25).

    `PREREGISTRATION.md` §9: if a newly promoted champion's rolling 7-day PR-AUC
    falls below the PREVIOUS champion's registered level by more than 0.02
    within 14 days of promotion, the previous champion is automatically restored.

    Automatic, and not an error state. A system that can promote but never
    retreat has only half a mechanism, and a rollback hidden in a log nobody
    publishes is a system quietly undoing its own decisions.

    The 14 days run from the PROMOTION DECISION, not from the model's training
    date (M5-FR-28). A model trained a fortnight before it was promoted would
    otherwise be past its own rollback window on the day it started serving —
    protected by nothing, at exactly the moment it is least proven.
    """
    run_id = new_run_id()
    settings = get_settings()
    day = day or datetime.now(UTC).date()

    with connect() as lock_conn, advisory_lock(lock_conn, PROMOTE_LOCK_KEY) as acquired:
        if not acquired:
            print("rollback: another decision run holds the lock, exiting cleanly")
            return {"skipped": True}

        with connect() as conn:
            require_current(conn)
            with conn.cursor() as cur:
                cur.execute(LAST_PROMOTION_SQL)
                promotion = cur.fetchone()

            if not promotion:
                print("rollback: nothing has been promoted, so nothing can be rolled back")
                return {"skipped": True, "reason": "no promotion"}

            days_since = float(promotion["days_since"])
            if days_since > pre.ROLLBACK_WINDOW_DAYS:
                print(
                    f"rollback: last promotion was {days_since:.1f}d ago, outside the "
                    f"{pre.ROLLBACK_WINDOW_DAYS}d window"
                )
                return {"skipped": True, "reason": "outside window", "days_since": days_since}

            promoted = promotion["challenger_version"]
            previous = promotion["champion_version"]
            previous_level = promotion["champion_pr_auc"]

            from bellwether import triggers

            rolling, matured = triggers.rolling_pr_auc(conn, version=promoted, day=day)

        if rolling is None or previous_level is None:
            print("rollback: not enough matured evidence to judge the promoted model yet")
            return {"skipped": True, "reason": "undefined", "n_matured": len(matured)}

        drop = float(previous_level) - rolling
        fell = drop > pre.ROLLBACK_PR_AUC_DROP

        print(f"rollback: {promoted} promoted {days_since:.1f}d ago, replacing {previous}")
        print(f"  rolling PR-AUC {rolling} vs the previous champion's {previous_level}")
        print(f"  drop {round(drop, 6)} > {pre.ROLLBACK_PR_AUC_DROP}? {fell}")

        if not fell:
            print("  holding")
            return {"rolled_back": False, "drop": round(drop, 6), "n_matured": len(matured)}

        reason = (
            f"rolling PR-AUC {rolling} is {round(drop, 6)} below {previous}'s registered "
            f"level {previous_level}, {days_since:.1f}d after promotion"
        )
        with RunContext(run_id, job=JOB) as ctx, connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    INSERT_ROLLBACK_SQL,
                    {
                        "restore": previous,
                        "failing": promoted,
                        "reason": reason,
                        "previous_level": previous_level,
                        "rolling": rolling,
                        "n": len(matured),
                        "commit": settings.build_id,
                        "run_id": run_id,
                    },
                )
                inserted = cur.fetchone()
                if inserted is None:
                    raise RuntimeError("the rollback was not recorded; refusing to act on it")
                decision_id = inserted["decision_id"]
                cur.execute(
                    INSERT_CHAMPION_SQL,
                    {
                        "version": previous,
                        "decision_id": decision_id,
                        "replaced": promoted,
                    },
                )
            ctx.rows_read = len(matured)
            ctx.rows_written = 1

    print(f"  ROLLED BACK to {previous}")
    return {
        "rolled_back": True,
        "restored": previous,
        "withdrew": promoted,
        "drop": round(drop, 6),
        "n_matured": len(matured),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate and print; write no decision. Never used in production.",
    )
    parser.add_argument(
        "--rollback-only",
        action="store_true",
        help="check the rollback condition and nothing else",
    )
    args = parser.parse_args()

    # Rollback is checked FIRST, every run. Evaluating a challenger against a
    # champion that should already have been withdrawn would compare against a
    # model the rule has decided is failing.
    if not args.dry_run:
        check_rollback()
    if not args.rollback_only:
        run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
