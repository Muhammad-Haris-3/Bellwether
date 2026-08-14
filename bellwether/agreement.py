"""How good a proxy is "was reverted" for "was a bad edit" (M7 §5, BQ-8).

Every label in this system says *this edit was reverted*. Every metric, every
promotion condition, the kill criterion — all of it is built on that standing in
for *this edit was bad*. Nothing has checked the substitution, and this is where
it gets checked.

**Computed on the random slice.** A κ over the ranked slice measures agreement
among edits the model already flagged, which is not the question. The queue
draws a fifth of its rows at random for exactly this, and the two are never
pooled without saying so.

**`unsure` is not missing data.** Those are the ambiguous cases — where the
proxy is most likely to disagree with a human, which is precisely what this
study is about. Dropping them selects on the outcome being studied, so both
treatments are computed and labelled as the different estimates they are.

**Refusals are recorded.** A run that declines to produce a figure writes a row
saying why. "We have not measured this" and "we measured it and there was not
enough" are different states, and an empty table cannot tell them apart.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from bellwether import maturity
from bellwether.config import get_settings
from bellwether.db import advisory_lock, connect
from bellwether.runlog import RunContext, new_run_id
from bellwether.schema import require_current

JOB = "agreement"
AGREEMENT_LOCK_KEY = 815_014

# M7-FR-14. Below this, no κ is published.
#
# A κ over thirty events is a number with a Greek letter on it, and it would be
# quoted. The threshold exists to stop it existing rather than to be reported
# alongside it with a caveat nobody reads.
MIN_LABELS_FOR_KAPPA = 100

# M7-FR-16. Below two reviewers, agreement is one person's judgement against the
# proxy, and inter-rater reliability cannot be computed at all — so there is no
# way to tell how much of a disagreement is the proxy being wrong and how much
# is that reviewer.
MIN_REVIEWERS_FOR_PROXY_CLAIM = 2

SLICES = ("random", "ranked", "all")
UNSURE_POLICIES = ("excluded", "as_good")

INSERT_SQL = """
INSERT INTO outcome.label_agreement
    (queue_slice, unsure_policy, n, n_reviewers, n_unsure, unsure_rate,
     both_positive, human_only, proxy_only, both_negative,
     kappa, observed_agreement, expected_agreement, refused_reason,
     maturity_hours, code_commit, run_id)
VALUES (%(slice)s, %(policy)s, %(n)s, %(reviewers)s, %(n_unsure)s, %(unsure_rate)s,
        %(both_positive)s, %(human_only)s, %(proxy_only)s, %(both_negative)s,
        %(kappa)s, %(po)s, %(pe)s, %(refused)s,
        %(maturity_hours)s, %(commit)s, %(run_id)s)
"""


def cohens_kappa(
    both_positive: int, human_only: int, proxy_only: int, both_negative: int
) -> tuple[float | None, float | None, float | None]:
    """κ, observed agreement, expected agreement.

    Returns (None, po, pe) when κ is undefined — which happens when expected
    agreement is 1.0, i.e. one of the raters put everything in a single class.
    That is not a κ of zero; it is a question the data cannot answer, and
    reporting 0.0 would read as "they agree no better than chance" when the
    truth is "there is no chance model to compare against".
    """
    n = both_positive + human_only + proxy_only + both_negative
    if n == 0:
        return None, None, None

    po = (both_positive + both_negative) / n

    human_positive = (both_positive + human_only) / n
    proxy_positive = (both_positive + proxy_only) / n
    pe = human_positive * proxy_positive + (1 - human_positive) * (1 - proxy_positive)

    if pe >= 1.0:
        return None, round(po, 6), round(pe, 6)
    return round((po - pe) / (1 - pe), 6), round(po, 6), round(pe, 6)


def confusion(rows: list[dict[str, Any]], *, unsure_policy: str) -> dict[str, int]:
    """Human verdict against the revert proxy.

    "bad" and "reverted" are the positive classes. `unsure` is either dropped or
    counted as `good_edit`, per the policy — never silently dropped, because
    dropping is itself a choice and it selects on the ambiguity this study is
    trying to measure.
    """
    counts = {"both_positive": 0, "human_only": 0, "proxy_only": 0, "both_negative": 0}
    for row in rows:
        verdict = row["verdict"]
        if verdict == "unsure":
            if unsure_policy == "excluded":
                continue
            human_bad = False
        else:
            human_bad = verdict == "bad_edit"

        proxy_bad = bool(row["proxy_reverted"])
        if human_bad and proxy_bad:
            counts["both_positive"] += 1
        elif human_bad:
            counts["human_only"] += 1
        elif proxy_bad:
            counts["proxy_only"] += 1
        else:
            counts["both_negative"] += 1
    return counts


def study(rows: list[dict[str, Any]], *, queue_slice: str, unsure_policy: str) -> dict[str, Any]:
    """One estimate: a slice, an `unsure` treatment, and what came out."""
    if queue_slice != "all":
        rows = [r for r in rows if r["queue_slice"] == queue_slice]

    reviewers = len({r["reviewer"] for r in rows})
    n_unsure = sum(1 for r in rows if r["verdict"] == "unsure")
    counts = confusion(rows, unsure_policy=unsure_policy)
    n_used = sum(counts.values())

    result: dict[str, Any] = {
        "slice": queue_slice,
        "policy": unsure_policy,
        "n": n_used,
        "reviewers": reviewers,
        "n_unsure": n_unsure,
        "unsure_rate": round(n_unsure / len(rows), 6) if rows else None,
        **counts,
        "kappa": None,
        "po": None,
        "pe": None,
        "refused": None,
    }

    if n_used < MIN_LABELS_FOR_KAPPA:
        result["refused"] = (
            f"{n_used} matured labels in the {queue_slice} slice; "
            f"{MIN_LABELS_FOR_KAPPA} required before a kappa is published"
        )
        return result

    kappa, po, pe = cohens_kappa(**counts)
    result["kappa"], result["po"], result["pe"] = kappa, po, pe
    if kappa is None:
        result["refused"] = (
            "expected agreement is 1.0 — one rater put every event in a single "
            "class, so there is no chance model to compare against"
        )
    elif reviewers < MIN_REVIEWERS_FOR_PROXY_CLAIM:
        # Not a refusal to publish; a refusal to call it a property of the proxy.
        result["refused"] = (
            f"{reviewers} reviewer(s): this is agreement between the proxy and "
            f"one person, not a measurement of the proxy itself"
        )
    return result


def run(*, maturity_seconds: int | None = None) -> dict[str, Any]:
    run_id = new_run_id()
    settings = get_settings()
    window = maturity_seconds or maturity.PROVISIONAL_MATURITY_SECONDS

    with connect() as lock_conn, advisory_lock(lock_conn, AGREEMENT_LOCK_KEY) as acquired:
        if not acquired:
            print("agreement: another run holds the lock, exiting cleanly")
            return {"skipped": True}

        with connect() as conn:
            require_current(conn)
            rows = conn.execute(
                "SELECT * FROM outcome.labels_for_agreement(%s)", (window,)
            ).fetchall()

        results = [
            study(rows, queue_slice=queue_slice, unsure_policy=policy)
            for queue_slice in SLICES
            for policy in UNSURE_POLICIES
        ]

        with RunContext(run_id, job=JOB) as ctx, connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    INSERT_SQL,
                    [
                        {
                            **result,
                            "maturity_hours": window // 3600,
                            "commit": settings.build_id,
                            "run_id": run_id,
                        }
                        for result in results
                    ],
                )
            ctx.rows_read = len(rows)
            ctx.rows_written = len(results)

    print(f"agreement: {len(rows):,} matured human labels")
    for result in results:
        if result["policy"] != "excluded":
            continue
        line = f"  {result['slice']:>7}  n={result['n']:<5} reviewers={result['reviewers']}"
        if result["kappa"] is not None:
            line += f"  kappa={result['kappa']}"
        print(line)
        if result["refused"]:
            print(f"           {result['refused']}")

    return {"n": len(rows), "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maturity-hours", type=int, default=None)
    args = parser.parse_args()
    run(maturity_seconds=args.maturity_hours * 3600 if args.maturity_hours else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
