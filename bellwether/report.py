"""The M0 measurements (M0-T4, M0-T5, FR-11).

Read-only. Prints the numbers the M0 summary has to contain, computed from the
live database rather than transcribed from a previous run.

Every figure carries its sample size, and every revert rate carries the
maturity it was measured at. A revert rate without a maturity is not a revert
rate — it is a lower bound wearing one's clothes, and it will keep rising for
as long as reverts keep arriving.
"""

from __future__ import annotations

import sys

from bellwether.db import fetch_all, fetch_one

RULE = "=" * 72


def _section(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def coverage() -> None:
    _section("COVERAGE")
    row = fetch_one(
        """
        SELECT count(*)                                        AS events,
               min(event_ts)                                   AS oldest,
               max(event_ts)                                   AS newest,
               round(EXTRACT(epoch FROM now() - max(event_ts)) / 3600, 1) AS newest_age_h,
               round(EXTRACT(epoch FROM now() - min(event_ts)) / 3600, 1) AS oldest_age_h
          FROM landing.rc_events
        """,
        readonly=True,
    )
    if not row or not row["events"]:
        print("  no events ingested")
        return
    print(f"  events              {row['events']:,}")
    print(f"  window              {row['oldest']}  ->  {row['newest']}")
    print(f"  age of newest       {row['newest_age_h']} h")
    print(f"  age of oldest       {row['oldest_age_h']} h")


def revert_rate_by_maturity() -> None:
    """VER-3. Rate by stratum, cut by how much time each edit has had.

    Cutting by maturity is the whole point: a single pooled number mixes edits
    that have had a week to be reverted with edits that have had ten minutes,
    and the result is neither a rate nor a bound on one.
    """
    _section("VER-3  REVERT RATE BY STRATUM AND MATURITY  (mw-reverted tag)")
    rows = fetch_all(
        """
        WITH aged AS (
            SELECT sampling_stratum,
                   CASE
                     WHEN now() - event_ts >= interval '48 hours' THEN '48h+'
                     WHEN now() - event_ts >= interval '24 hours' THEN '24-48h'
                     WHEN now() - event_ts >= interval '6 hours'  THEN '6-24h'
                     WHEN now() - event_ts >= interval '1 hour'   THEN '1-6h'
                     ELSE '<1h'
                   END AS maturity,
                   ('mw-reverted' = ANY(tags)) AS reverted
              FROM landing.rc_events
        )
        SELECT maturity, sampling_stratum,
               count(*) AS n,
               count(*) FILTER (WHERE reverted) AS reverted,
               round(100.0 * count(*) FILTER (WHERE reverted) / count(*), 2) AS pct
          FROM aged
         GROUP BY maturity, sampling_stratum
         ORDER BY CASE maturity
                    WHEN '<1h' THEN 1 WHEN '1-6h' THEN 2 WHEN '6-24h' THEN 3
                    WHEN '24-48h' THEN 4 ELSE 5 END,
                  sampling_stratum
        """,
        readonly=True,
    )
    print(f"  {'maturity':<10}{'stratum':<14}{'n':>8}{'reverted':>10}{'rate':>9}")
    for r in rows:
        print(
            f"  {r['maturity']:<10}{r['sampling_stratum']:<14}"
            f"{r['n']:>8,}{r['reverted']:>10,}{r['pct']:>8}%"
        )
    print("\n  Rates below 48h are lower bounds. Reverts continue to arrive.")


def detection_latency() -> None:
    """VER-1. How long after an edit its revert becomes visible to us.

    Reported alongside the ingestion lag it cannot go below. Backfilling a
    nine-hour-old window makes every detection latency at least nine hours, and
    the resulting figure describes when the backfill ran — not how fast reverts
    arrive. Printing it without that floor beside it would be the exact failure
    this project exists to avoid, so the two are shown together and the reading
    is left to whoever is looking.
    """
    _section("VER-1  DETECTION LATENCY  (edit -> we could see the outcome)")

    floor = fetch_one(
        """
        SELECT round((percentile_cont(0.5) WITHIN GROUP (
                   ORDER BY EXTRACT(epoch FROM ingested_at_utc - event_ts)) / 60.0)::numeric,
               1) AS median_lag_min
          FROM landing.rc_events
        """,
        readonly=True,
    )
    if floor and floor["median_lag_min"] is not None:
        print(f"  median ingestion lag  {floor['median_lag_min']} min   <- the floor below")
        print("  any detection latency at or near this measures the backfill, not the wiki\n")

    rows = fetch_all(
        """
        -- percentile_cont returns double precision, which has no two-argument
        -- round(). The cast is required, not stylistic.
        SELECT label_source,
               count(*) AS n,
               round(min(detection_latency_seconds) / 60.0, 1)  AS min_min,
               round((percentile_cont(0.5) WITHIN GROUP (
                     ORDER BY detection_latency_seconds) / 60.0)::numeric, 1) AS p50_min,
               round((percentile_cont(0.9) WITHIN GROUP (
                     ORDER BY detection_latency_seconds) / 60.0)::numeric, 1) AS p90_min
          FROM outcome.labels
         WHERE label
         GROUP BY label_source
         ORDER BY label_source
        """,
        readonly=True,
    )
    if not rows:
        print("  no positive labels yet")
        return
    print(f"  {'source':<14}{'n':>8}{'min':>10}{'p50':>10}{'p90':>10}   (minutes)")
    for r in rows:
        print(
            f"  {r['label_source']:<14}{r['n']:>8,}"
            f"{r['min_min']:>10}{r['p50_min']:>10}{r['p90_min']:>10}"
        )


def checkpoint_grid() -> None:
    """The raw material for M2's survival estimate, including the negatives."""
    _section("CHECKPOINT GRID  (M2 input — negatives retained on purpose)")
    rows = fetch_all(
        """
        SELECT checkpoint_seconds,
               count(*) AS checks,
               count(*) FILTER (WHERE had_reverted_tag) AS positive,
               count(*) FILTER (WHERE rev_missing)      AS deleted,
               round((percentile_cont(0.5) WITHIN GROUP (
                     ORDER BY age_seconds) / 3600.0)::numeric, 2) AS median_age_h
          FROM outcome.label_checks
         GROUP BY checkpoint_seconds
         ORDER BY checkpoint_seconds
        """,
        readonly=True,
    )
    if not rows:
        print("  no checks recorded yet")
        return
    print(f"  {'checkpoint':>12}{'checks':>9}{'positive':>10}{'deleted':>9}{'median age':>12}")
    for r in rows:
        label = f"{r['checkpoint_seconds'] // 3600}h"
        print(
            f"  {label:>12}{r['checks']:>9,}{r['positive']:>10,}"
            f"{r['deleted']:>9,}{r['median_age_h']:>11}h"
        )


def path_agreement() -> None:
    """FR-11. Where the two independent label paths disagree.

    Disagreement is reported, never resolved in place. Each path has a known
    failure mode — the secondary under-counts rollbacks by design, the primary
    depends on a deferred job — and the size of the gap is evidence about both.
    """
    _section("FR-11  LABEL PATH AGREEMENT")

    # Restricted to edits the primary path has actually looked at.
    #
    # Comparing over everything ingested counts "the primary path has not got
    # there yet" as disagreement, which makes the two paths look like they
    # contradict each other when they have simply not both spoken. That
    # comparison would understate agreement by whatever fraction of the backlog
    # is unchecked, and the fraction changes every run — so the figure would
    # drift for reasons that have nothing to do with either path.
    row = fetch_one(
        """
        WITH checked AS (SELECT DISTINCT revid FROM outcome.label_checks),
             p AS (SELECT revid FROM outcome.labels
                    WHERE label_source = 'mw_reverted' AND label),
             s AS (SELECT revid FROM outcome.labels
                    WHERE label_source = 'revert_tag'  AND label)
        SELECT (SELECT count(*) FROM checked)                          AS checked,
               (SELECT count(*) FROM p JOIN checked USING (revid))     AS primary_pos,
               (SELECT count(*) FROM s JOIN checked USING (revid))     AS secondary_pos,
               (SELECT count(*) FROM p JOIN s USING (revid)
                                       JOIN checked USING (revid))     AS both,
               (SELECT count(*) FROM p JOIN checked USING (revid)
                 WHERE revid NOT IN (SELECT revid FROM s))             AS primary_only,
               (SELECT count(*) FROM s JOIN checked USING (revid)
                 WHERE revid NOT IN (SELECT revid FROM p))             AS secondary_only,
               (SELECT count(*) FROM s
                 WHERE revid NOT IN (SELECT revid FROM checked))       AS secondary_unchecked
        """,
        readonly=True,
    )
    if not row or not row["checked"]:
        print("  no edits checked by the primary path yet")
        return

    print(f"  edits checked by both paths             {row['checked']:>8,}")
    print(f"  positive, primary (mw-reverted)         {row['primary_pos']:>8,}")
    print(f"  positive, secondary (revert tags)       {row['secondary_pos']:>8,}")
    print(f"  agreed by both                          {row['both']:>8,}")
    print(f"  primary only                            {row['primary_only']:>8,}")
    print(f"  secondary only                          {row['secondary_only']:>8,}")
    print(f"\n  secondary positives not yet checked     {row['secondary_unchecked']:>8,}")
    print("  (excluded above — not disagreement, just an unfinished queue)")
    if row["secondary_only"]:
        print(
            "\n  Secondary-only positives are expected: the reverting edit is\n"
            "  tagged at once, while mw-reverted waits on a deferred job."
        )


def run_health() -> None:
    _section("RUN LOG")
    rows = fetch_all(
        """
        SELECT job, status, count(*) AS runs, max(started_at_utc) AS last_run
          FROM landing.run_log
         GROUP BY job, status
         ORDER BY job, status
        """,
        readonly=True,
    )
    for r in rows:
        print(f"  {r['job']:<18}{r['status']:<10}{r['runs']:>5}   last {r['last_run']}")


def main() -> int:
    coverage()
    revert_rate_by_maturity()
    detection_latency()
    checkpoint_grid()
    path_agreement()
    run_health()
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
