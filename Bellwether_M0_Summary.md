# Bellwether — M0 Summary

**Milestone:** M0 — Label loop proof and walking skeleton
**Started / completed:** 2026-08-13
**Status:** 8 of 9 tasks complete. A-4 (deliberate outage) in progress.
**Live:** [bellwether-phi.vercel.app](https://bellwether-phi.vercel.app) ·
[API](https://bellwether-fyyz.onrender.com/health)

---

## 1. The question M0 existed to answer

> Can the system record a score for an edit, and then automatically discover,
> later, whether that edit was reverted — with no human involved?

**Yes.** `mw-reverted` appeared on 128 of 4,000 edits in a 5–6 hour window,
retrieved by a scheduled job, joined back to events ingested earlier. The
kill criterion KC-1 is retired.

The project is worth building. Everything below is what it cost to find that
out, and what changed along the way.

---

## 2. What was built

| Task | Delivered |
|---|---|
| T1 | Public repo, CI on Postgres 18 with lint, format, types, 72 tests |
| T2 | Cursor-based ingestion, idempotent by primary key, bounded per run |
| T3 | Two schemas, append-only grants, one-command bootstrap that verifies itself |
| T4 | Two independent label paths, with the checks that find nothing retained |
| T5 | Base rates at 48h maturity |
| T6 | Lift Wing benchmark probed and characterised |
| T7 | Scheduled workflows, asserted in the test suite |
| T8 | Read-only API and live status page, both verified in production |
| T9 | `PREREGISTRATION.md`, committed while `model_registry` did not exist |

---

## 3. Findings that changed the design

**English Wikipedia no longer exposes IP addresses.** Logged-out editors are
issued temporary accounts (`~2026-44334-20`) carrying a `temp` flag, and `anon`
was set on **0 of 2,498** live edits. The sampling frame in SRS §6.3 was keyed
on `anon`. It would have placed 100% of traffic in the registered stratum and
sampled away most of the positive class before training began. The frame now
splits on logged-out versus logged-in, using the API's own flag rather than a
pattern match on the username.

**Undo summaries name their target as a link.** `Undid revision
[[Special:Diff/1367883184|1367883184]]`, not a bare number. The first parser
matched **0 of 97** undos and reported a clean run while deriving nothing at
all. The job now surfaces its count of underivable targets rather than leaving
it implicit — a silent zero is worse than a crash.

**Tags applied after an edit appear on a later `recentchanges` poll** (VER-5).
That was an open question in the SRS and the answer is worth ten times the
request budget: a window can be re-swept 500 revisions at a time instead of 50
per `prop=revisions` call. M1 switches to it.

**GitHub's scheduled workflows under-deliver.** The first scheduled run fired
**42 minutes** after the workflow landed on `main`, and a `*/10` schedule has at
times run closer to hourly. No data is lost — that is precisely what the cursor
design absorbs — but M1's throughput planning must not assume 144 runs a day.

---

## 4. What was measured

### Base rates (VER-3), 20,000 edits at 48h+ maturity

| Stratum | n | Reverted | Rate |
|---|---|---|---|
| Logged out | 2,472 | 550 | **22.25%** |
| Registered | 17,528 | 572 | **3.26%** |
| Overall | 20,000 | 1,122 | **5.61%** |

### Volume (VER-2)

~56–62 edits/minute in main namespace, non-bot — roughly **81–90k/day**.

### Label paths (FR-11), over edits both paths could see

| | |
|---|---|
| Secondary precision | **100%** (6 of 6 agreed with the primary) |
| Secondary recall | **~19%** (6 of 32) |

Conservative by design: it never claims a revert that did not happen and openly
misses ones that did. The gap is now a measured number rather than an
assumption, which is the reason for running both.

### The benchmark (VER-4)

Lift Wing's `revertrisk-language-agnostic` answers without authentication. On 16
revisions — 8 reverted, 8 not — the scores separate completely: median 0.928
against 0.177, with the lowest-scoring revert above the highest-scoring
survivor.

Sixteen points is not an AUC. It is enough to record, before any model exists,
that **Bellwether is unlikely to beat it**, and `PREREGISTRATION.md` §10 says so
in advance precisely so the comparison cannot later be dropped or swapped for a
weaker opponent.

---

## 5. A hypothesis for M2

Comparing each stratum at under an hour against its mature rate:

| Stratum | <1h | 48h+ | Share landing in hour one |
|---|---|---|---|
| Logged out | 9.89% | 22.25% | ~44% |
| Registered | 0.69% | 3.26% | ~21% |

**Reverts of registered editors appear to arrive about twice as slowly.**

These are different samples from different days, so day-of-week and
time-of-day are uncontrolled. It is a hypothesis, not a finding. But if it
holds, a single global maturity window is wrong: it would systematically
undercount registered-user reverts, and the bias would land exactly on the
split the model ranks by. `PREREGISTRATION.md` §6 therefore already permits
per-stratum windows, decided by the M2 survival curve.

---

## 6. Ten bugs, and what each nearly cost

Recorded in full because the pattern matters more than any individual entry.

| # | Bug | Why it was dangerous |
|---|---|---|
| 1 | `SET ROLE` failure and statement refusal raise the same error | Six append-only probes "passed" having executed nothing. Only the one probe expecting success revealed it |
| 2 | Power calculation modelled the challenger as champion-plus-a-constant | Effectively ρ=1. Reported a required sample of 169 events; real power there was 0.36 |
| 3 | Workflow tests read `doc["on"]` | YAML 1.1 parses bare `on` as boolean `True`, so every schedule assertion passed vacuously |
| 4 | Test fixtures truncated the working database | A green run and a wiped database look identical from the terminal. Cost four thousand ingested events |
| 5 | Bootstrap verified with passwords it had deliberately not applied | Announced the append-only guarantee broken on a database where it held perfectly |
| 6 | Detection latency printed without the ingestion-lag floor | Backfilling a 9-hour-old window makes every latency ≥9 hours; the figure described the backfill, not the wiki |
| 7 | Path agreement ignored the secondary path's lookback | Reported 3% recall for a component that had never been shown the data |
| 8 | `requirements-dev.txt` omitted the serving requirements | CI failed on `fastapi` while local passed, because local had it installed by hand |
| 9 | Frontend joined a base URL ending in `/` | `//stats` → 404, which reads as "the API is broken" when the API is fine |
| 10 | Status cell coloured every non-success red | `partial` is a healthy outcome; a permanent red alarm teaches you to ignore the colour |

**Six of these produce a wrong number or a false pass rather than an error.**
That is the failure mode this project exists to defeat, and it turned up ten
times in its own first milestone. Every one was caught by a verification step
that existed only because something similar had been anticipated.

---

## 7. Production verification

Per the Definition of Done, verified against the deployed system:

| Claim | Evidence |
|---|---|
| Append-only holds on Neon | 18 catalogue privileges correct, 6 forbidden operations refused, PostgreSQL 18.4 |
| Pipeline runs unattended | Scheduled Ingest and Label runs succeeding with no manual trigger |
| API serves live data | `/health` `status: ok`, `database_reachable: true`, read-only role, pooled endpoint |
| Page renders live data | 35,956 events, matured rates with sample sizes, staleness per job |
| Pre-registration predates any model | `model_registry` does not exist; committed 2026-08-13 |

---

## 8. Outstanding

**A-4, the deliberate outage.** Ingest disabled 2026-08-13 ~14:59Z with the
cursor at `2026-08-13T14:51:27Z` and 35,956 events. After a two-hour gap and
re-enabling, the backfill must close it with no hole and no duplicates.

`/stats` now publishes `coverage` — gap count, largest gap, hours covered
against hours spanned — so this is checkable by anyone, not only by whoever
holds database credentials.

---

## 9. Carried into M1

1. **Sampling rate `R`**, set from the measured 81–90k/day against the storage
   ceiling.
2. **The cheaper label path** (VER-5), which is also the fix for the growing
   backlog: `label_checks` sat at 18,072 while events passed 35,000.
3. **Gap-filling**, now that `/stats` exposes what needs filling.
4. **Retention**, before the free tier fills.
5. **A cadence assumption grounded in observation**, not in the cron expression.
