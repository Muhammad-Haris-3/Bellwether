# Bellwether — M1 Specification

**Milestone:** M1 — Durable ingestion within a fixed storage budget
**Date:** 2026-08-13
**Status:** Not started
**Depends on:** `Bellwether_SRS_v1.0.md`, `Bellwether_M0_Summary.md`

---

## 1. The question M1 answers

M0 proved the loop closes. M1 answers the question that decides whether it can
keep running:

> **How much of Wikipedia can this project afford to remember, and for how
> long?**

Everything else in M1 — the sampling rate, retention, gap-filling, the cheaper
label path — follows from that arithmetic.

---

## 2. The budget, measured

**Neon Free is 0.5 GB per project.** NFR-4 caps usage at 80%, so the working
budget is **400 MB**.

Measured on real ingested data, 2026-08-13:

| Table | Bytes/row | Composition |
|---|---|---|
| `rc_events` | **372** | 264 heap + 91 index + overhead |
| `label_checks` | ~120 | narrow; index-dominated |
| `labels` | ~150 | narrow |

Column-level, per event: fixed columns 120 B, `title` 22.6 B, `comment` 48.3 B,
`tags` 40.3 B, `user_name` 10.9 B.

### 2.1 The SRS's retention plan does not fit

SRS §6.5 specifies 120-day retention on raw events. At 372 B/row that permits
**9,000 events/day** — and SRS §6.3 specifies keeping **100% of logged-out
edits**, which is roughly **14,000/day** on its own.

**The sampling frame and the retention policy in the SRS are mutually
impossible.** M1 resolves it. This is the kind of contradiction that only
surfaces when someone measures rather than estimates, and it is why M1 exists.

### 2.2 What a single event really costs

Steady state, including what M3 will add:

| Component | Rows/event | Bytes | Retained |
|---|---|---|---|
| `rc_events` | 1 | 300 (after §4) | 30 days |
| `label_checks` | 1.4 (after §5) | 120 | 30 days |
| `labels` | 1 | 150 | 90 days |
| `predictions` (M3) | 2 — champion + shadow | 120 | 90 days |

Total ≈ **49,000 bytes per event per day of the retention mix**, giving a
sustainable rate of about **8,000 events/day** inside 400 MB.

---

## 3. The sampling frame, revised

M0 measured ~81–90k main-namespace non-bot edits/day, of which **15.7%** are
logged out.

| Stratum | Population/day | Rate | Kept/day |
|---|---|---|---|
| Logged out | ~14,000 | **50%** | 7,000 |
| Registered | ~76,000 | **3%** | 2,280 |
| **Total** | ~90,000 | | **9,280** |

```
INCLUDE  namespace = 0 AND type = edit AND bot = false
AND      ( ( anon OR temp ) AND hash(revid) mod 100 < 50    -- stratum A
           OR hash(revid) mod 100 < 3 )                     -- stratum B
```

**This changes the SRS.** §6.3 specified stratum A at 100%; it cannot be
afforded alongside an evidence trail. Sampling it is preferable to the
alternatives — shortening the evidence window, or dropping shadow predictions —
because the frame stays a documented probability sample either way, and
weighting corrects for it. What cannot be corrected for is evidence that was
never kept.

Deterministic by hash of `revid`, so the frame is reproducible and re-running
ingestion selects the identical set.

**Yield check.** 7,000 × 22.25% + 2,280 × 3.26% ≈ **1,630 matured positives per
day**. `PREREGISTRATION.md` P-3 requires 2,500 before any promotion, reached in
under two days — comfortably inside P-4's seven-day minimum, so the sample size
requirement never becomes the binding constraint on a decision.

| # | Requirement |
|---|---|
| M1-FR-1 | Ingestion shall apply the frame above, recording stratum and weight at observation time |
| M1-FR-2 | Weights shall be the inverse sampling probability, so population estimates are recoverable |
| M1-FR-3 | Every published rate shall be available both raw and population-weighted |

---

## 4. Slimming `rc_events`

Three changes, each justified by measurement rather than instinct:

| Change | Saving | Rationale |
|---|---|---|
| Drop the `(event_ts, revid)` index | ~25 B | Redundant with the `event_ts` index plus primary-key lookup |
| `tags text[]` → `smallint[]` + a `tag_names` dimension | ~50 B | Only **67 distinct tags** exist. The GIN index shrinks with the payload |
| Keep `title` as-is | — | 5,000 events span 3,927 distinct pages — **1.27 events/page**. A page dimension would save nothing. Measured, not assumed |

Target: **372 → ~300 bytes/row.**

| # | Requirement |
|---|---|
| M1-FR-4 | Tags shall be stored as ids against a dimension table, with the text recoverable by join |
| M1-FR-5 | The redundant composite index shall be removed and the remaining set justified against the queries that use it |

---

## 5. The checkpoint grid becomes a cohort

M0 checks every event at five ages. At steady state that is 5 rows per event and
the single largest storage line after `rc_events`.

The grid exists to estimate **one curve** in M2. It does not need to run on
every event forever.

| Population | Checks |
|---|---|
| A fixed 10% cohort, by hash of `revid` | All five checkpoints |
| Everything else | One check, at the maturity window |

Rows per event fall from 5 to **1.4**. The cohort is deterministic, so the
survival estimate is computed on a documented probability sample rather than on
whatever the job happened to reach.

| # | Requirement |
|---|---|
| M1-FR-6 | The maturity cohort shall be deterministic by hash and disjoint from the sampling decision |
| M1-FR-7 | Non-cohort events shall receive exactly one check, at the maturity window |

---

## 6. Retention, and how the evidence survives it

SRS §6.5 says predictions and labels are retained **indefinitely** because they
are the evidence. At 0.5 GB that is not possible, and pretending otherwise would
mean discovering it when the database fills.

**Resolution — seal, then prune.** Carry forward the mechanism from GridCast:
each month, a hash of that month's evidence rows is committed to the public
repository as `seals/YYYY-MM.json`. The rows may then age out of the database
while the proof that they were not altered persists in git history, publicly,
verifiable by someone with access to neither the database nor the author.

| Table | Retention | Why |
|---|---|---|
| `rc_events` | 30 days | Raw material; regenerable in principle, not evidence |
| `label_checks` | 30 days, cohort rows 180 days | Cohort rows are the survival study |
| `labels` | 90 days | Sealed monthly before pruning |
| `predictions` (M3) | 90 days | Sealed monthly before pruning |
| `metrics`, `model_decisions`, `run_log` | **indefinite** | Small, and they are the decision record |
| `seals/` in git | **forever** | The point |

| # | Requirement |
|---|---|
| M1-FR-8 | A retention job shall run on a schedule, with a dry-run mode, logging every deletion count |
| M1-FR-9 | No evidence row shall be deleted before the month containing it has been sealed |
| M1-FR-10 | Foreign keys from `labels` to `rc_events` shall be removed, so evidence retention is not chained to raw retention |
| M1-FR-11 | A storage report shall be exposed by the API and shall alert above 80% of budget |

---

## 7. Gap detection and healing

`/stats` already exposes gaps (M0). M1 acts on them.

| # | Requirement |
|---|---|
| M1-FR-12 | A gap-fill job shall detect gaps over 10 minutes within the retention window and backfill them, oldest first |
| M1-FR-13 | Gap-filling shall never move the ingestion cursor backwards |
| M1-FR-14 | Gaps older than the `recentchanges` retention horizon shall be recorded as permanent and excluded from coverage claims rather than retried forever |

M0 proved a 3h09m outage self-heals on the next run. M1-FR-12 covers the case
that run cannot: a gap longer than one run's page budget, or one that opened
behind the cursor.

---

## 8. The cheaper label path

VER-5 established that `recentchanges` re-polling returns tags applied after the
edit — **500 revisions per request instead of 50**.

| # | Requirement |
|---|---|
| M1-FR-15 | The primary label harvest shall re-poll `recentchanges` by window |
| M1-FR-16 | `prop=revisions` shall remain as the fallback for events older than the wiki's `recentchanges` horizon |
| M1-FR-17 | Agreement between the two retrieval methods shall be measured before the cheaper one becomes primary |

M0 left `label_checks` at 30,072 while events passed 49,580 — the labeller is
losing ground. The frame in §3 cuts the inflow by 90% and this cuts the cost per
check by 90%.

---

## 9. Cadence, grounded in observation

M0 measured GitHub's scheduler firing **42 minutes** after a workflow landed,
and a `*/10` schedule running closer to hourly.

| # | Requirement |
|---|---|
| M1-FR-18 | Page budgets per run shall be sized for the **observed** cadence, not the nominal one |
| M1-FR-19 | The status page shall show observed inter-run intervals, so the assumption stays visible |

---

## 10. Acceptance criteria

| # | Criterion |
|---|---|
| B-1 | The frame in §3 is applied, with stratum and weight recorded at observation time |
| B-2 | Population-weighted and raw rates are both published, and differ as the frame predicts |
| B-3 | Projected 90-day storage, computed from measured row sizes, sits below 400 MB |
| B-4 | A deliberately introduced gap is detected and healed by the gap-fill job, not by the next ordinary run |
| B-5 | The retention job deletes only what its dry run predicted, and nothing unsealed |
| B-6 | A month is sealed, the seal committed, and a pruned month verifiable from git alone |
| B-7 | The label path re-polls `recentchanges`, with measured agreement against `prop=revisions` before promotion to primary |
| B-8 | `label_checks` stops falling behind: checks due, at the end of a day, are fewer than at its start |
| B-9 | Storage is reported by the API and the projection is visible on the status page |

---

## 11. What M1 must not become

- Building features or a model. That is M2 and M3.
- Optimising storage past what the budget requires. The target is 400 MB, not
  the smallest possible database.
- Treating the revised frame as provisional. Once ingestion runs under it, the
  frame is part of every downstream estimate; changing it later invalidates
  comparisons across the change.
