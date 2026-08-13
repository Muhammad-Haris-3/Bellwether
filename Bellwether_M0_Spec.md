# Bellwether — M0 Specification

**Milestone:** M0 — Label loop proof and walking skeleton
**Author:** Muhammad Haris Khokhar
**Date:** 2026-08-13
**Status:** Not started
**Depends on:** `Bellwether_SRS_v1.0.md`

---

## 1. The one question

> **Can I record a score for an edit, and then automatically discover, later,
> whether that edit was reverted — without any human telling the system?**

Everything else in Bellwether is engineering. This is the only thing that can
kill it. M0 exists to answer this in days, not to discover it in month two.

**No modelling work of any kind happens in M0.** No features, no training, no
sampling rate, no evaluation. If it is not needed to answer the question above
or to stand up the thinnest deployable skeleton, it is out of scope.

---

## 2. Scope

### In scope

- Ingest a bounded window of English Wikipedia main-namespace edits into
  PostgreSQL with a durable cursor.
- Re-query those same revisions later and harvest the `mw-reverted` tag.
- Prove a specific revision went from *no label* to *reverted* with no human
  action, and record how long that took.
- Implement the **secondary** label path as an independent cross-check.
- Measure the facts the SRS marked unverified: VER-1 through VER-5.
- Stand up the walking skeleton: repo, CI, schema, one scheduled workflow, a
  deployed API, a deployed page showing live counts.
- Commit `PREREGISTRATION.md` before any model exists.

### Explicitly out of scope

Features, `editor_state`, the knowability guard, any model, scoring, metrics,
drift detection, promotion machinery, authentication, roles, the review queue,
retention, the final sampling rate. All belong to M1+.

### Deliberate simplification

M0 ingests **100% of main-namespace non-bot edits within its test window**, not
a sample. The window is short and bounded, so storage is not at risk, and full
ingestion gives a clean measurement of true volume (VER-2) and base rate (VER-3)
from which M1 sets the sampling rate `R`. Sampling before measuring would be
guessing.

---

## 3. Tasks

### M0-T1 — Repository and CI skeleton

Create `Bellwether` as a **public** GitHub repository (public is a requirement,
not a preference — unlimited Actions minutes depend on it).

```
bellwether/
  pipeline/            # Python: ingestion, labelling, jobs
  api/                 # FastAPI, read-only role
  web/                 # Next.js
  sql/                 # schema.sql, grants.sql
  scripts/             # bootstrap_database.py
  .github/workflows/   # ci.yml, ingest.yml, label.yml
  PREREGISTRATION.md
  METHODS.md
  README.md
```

CI on every push: ruff, mypy, pytest, `next build`. Green CI is a precondition
for every later task.

**Done when:** CI is green on `main`.

---

### M0-T2 — Ingest one window  *(resolves VER-2)*

Implement `pipeline/ingest.py` against DS-1.

```
GET https://en.wikipedia.org/w/api.php
  ?action=query&list=recentchanges&format=json&formatversion=2
  &rcnamespace=0
  &rctype=edit
  &rcshow=!bot
  &rcprop=ids|timestamp|title|user|userid|comment|flags|sizes|tags|patrolled
  &rclimit=500
  &rcdir=newer
  &rcstart=<cursor>
```

Requirements:

- `User-Agent: Bellwether/0.1 (https://github.com/Muhammad-Haris-3/Bellwether; hariskhokhar975@gmail.com) python-requests/x.y`
  — required for the 200 req/min tier; without it the limit is 10 req/min.
- Page with `rccontinue` until the window is exhausted.
- Cursor persisted in a `cursors` table, advanced **only after** the batch
  commits, so a crash re-reads rather than skips.
- `INSERT ... ON CONFLICT (revid) DO NOTHING` — idempotency by constraint, not
  by application logic.
- Sleep/backoff to stay under 40 req/min, well inside NFR-2. Honour `Retry-After`.

**Measure and record in the summary (VER-2):** edits per hour, split by
anonymous vs. registered, over at least one full 24-hour cycle. This number sets
`R` in M1.

**Done when:** two consecutive runs over an overlapping window insert zero
duplicates, and the cursor advances correctly across a deliberately killed run.

---

### M0-T3 — Schema and append-only grant

`sql/schema.sql` — the M0 subset only:

| Table | Columns (essential) |
|---|---|
| `run_log` | `run_id`, `job`, `started_at`, `finished_at`, `window_start`, `window_end`, `rows_in`, `api_calls`, `status`, `error` |
| `cursors` | `job`, `position`, `updated_at` |
| `rc_events` | `revid` PK, `old_revid`, `rcid`, `event_ts`, `ns`, `title`, `user_name`, `user_id`, `is_anon`, `is_minor`, `is_patrolled`, `comment`, `oldlen`, `newlen`, `tags` `text[]`, `ingested_at` |
| `labels` | `label_id`, `revid`, `label` bool, `label_source` (`mw_reverted` / `revert_tag`), `first_observed_at`, `revert_latency_seconds`, `revert_revid` nullable, `observed_run_id` |

Constraints that must exist from day one:

```sql
ALTER TABLE labels ADD CONSTRAINT labels_latency_nonneg
  CHECK (revert_latency_seconds IS NULL OR revert_latency_seconds >= 0);

ALTER TABLE rc_events ADD CONSTRAINT rc_events_ingested_after_event
  CHECK (ingested_at >= event_ts);
```

`sql/grants.sql` — establish the pattern now, while it is cheap:

- `bellwether_writer` (used by GitHub Actions): `INSERT` on all tables,
  `UPDATE`/`DELETE` on `cursors` and `run_log` **only**.
- `bellwether_readonly` (used by the Render API): `SELECT` only.

`scripts/bootstrap_database.py "<owner-url>"` applies schema and grants,
verifies the append-only guarantee by attempting a forbidden `UPDATE` on
`labels` as `bellwether_writer` and asserting it fails, then prints the
read-only URL to paste into Render.

**Done when:** the bootstrap script's forbidden-write assertion passes against
the production Neon instance, not just locally.

---

### M0-T4 — Harvest labels  *(resolves VER-1 and VER-5 — the critical task)*

Implement `pipeline/label.py`, **primary path**:

```
GET https://en.wikipedia.org/w/api.php
  ?action=query&prop=revisions&format=json&formatversion=2
  &revids=<up to 50 comma-separated revids>
  &rvprop=ids|timestamp|tags
```

For each revision returned, `mw-reverted` present in `tags` ⇒ `label = true`.

Selection policy: re-check every ingested revision at approximately T+1h, T+6h,
T+24h, T+48h and T+7d after its `event_ts`. This is the raw material for the
maturity analysis in M2 — do not collapse it to a single check.

**Also implement the secondary path** (`pipeline/label_secondary.py`): watch the
ingested feed for edits carrying `mw-undo`, `mw-rollback` or `mw-manual-revert`,
and map each reverting edit back to the revisions it reverted, via the page's
revision history and `old_revid` chain. Write these as
`label_source = 'revert_tag'`.

Both paths write independent rows. **Never** reconcile them silently.

**Explicit measurements for the M0 summary:**

| Question | Why it matters |
|---|---|
| Does `mw-reverted` appear on a revision that carried no tag at ingestion? | VER-1 — the entire project |
| Median and p90 delay between the reverting edit and the tag becoming visible | Sets M2's maturity work |
| Does re-polling `recentchanges` over an old window show updated tags? | VER-5 — a cheaper harvest path if true |
| Agreement rate between primary and secondary paths | FR-11 baseline |

**Done when:** the summary names at least one specific `revid` with (a) the
timestamp it was ingested carrying no revert tag, and (b) the timestamp a
scheduled job discovered its `mw-reverted` tag, with no human action between —
and the same event is confirmed independently by the secondary path.

---

### M0-T5 — Base rate  *(resolves VER-3)*

From at least 24 hours of ingested events matured to T+48h, report the observed
revert rate overall and split by anonymous vs. registered.

**This number is a precondition for `PREREGISTRATION.md`**, because the minimum
sample size for promotion (FR-31) is a power calculation, and the base rate is
an input to it. Do not write the pre-registration before this task completes.

---

### M0-T6 — Lift Wing probe  *(resolves VER-4, non-blocking)*

Establish, and record in the summary, the exact endpoint path, request body,
authentication requirement and rate limit for the language-agnostic **Revert
Risk** model. Score a handful of already-labelled revisions by hand and sanity
check the direction.

Non-blocking: the benchmark itself is M4. If the endpoint requires
authentication or a token that is not free, record that finding and drop
FR-26 to optional. Do not let this task delay M0.

---

### M0-T7 — Scheduled execution

`.github/workflows/ingest.yml` — every 10 minutes.
`.github/workflows/label.yml` — every 30 minutes.

Both must:

- take a PostgreSQL advisory lock and exit cleanly if already held (NFR-6);
- write a `run_log` row on **every** outcome including failure;
- complete inside 10 minutes (NFR-5);
- read `BELLWETHER_WRITER_DATABASE_URL` from repository secrets.

Note the known behaviour: GitHub's scheduled workflows are best-effort and can
be delayed under load. This is exactly what the cursor design absorbs — verify
it by deliberately disabling the workflow for two hours and confirming the next
run backfills the gap with no loss and no duplicates.

**Done when:** 24 hours of unattended runs complete with a continuous
`run_log`, no gaps in `rc_events`, and the deliberate-outage backfill verified.

---

### M0-T8 — Deployed skeleton

- **API** (Render, free, Docker, `bellwether_readonly` credential only):
  `GET /health` — status, database reachability, **no secrets** (NFR-9);
  `GET /stats` — events ingested, labels harvested, observed revert rate,
  last successful run per job, and staleness in minutes.
- **Web** (Vercel): one page rendering `/stats`, with an explicit
  cold-start loading state and a visible staleness indicator (NFR-7, NFR-11).

**Done when:** the numbers are visible in a browser at a public URL and change
without intervention between two visits an hour apart.

---

### M0-T9 — Pre-registration

Commit `PREREGISTRATION.md` **before M1 begins and before any model exists**,
fixing at minimum:

| Item | Must state |
|---|---|
| Primary metric | PR-AUC on matured predictions (rationale: rare positive class) |
| Secondary metrics | Precision@k for the operational k, Brier, calibration error |
| Promotion margin | The minimum absolute PR-AUC improvement that counts |
| Statistical test | Paired bootstrap on matched matured events; iterations; α |
| Minimum sample | Matured labelled events required before promotion — **derived from the M0-T5 base rate by power calculation, with the calculation shown** |
| Minimum shadow duration | Elapsed wall-clock minimum, independent of sample |
| Calibration tolerance | How much calibration degradation vetoes an otherwise-winning challenger |
| Segment tolerance | Maximum permitted regression in any §7.5 segment |
| Drift thresholds | PSI trigger levels, and consecutive windows required |
| Decay trigger | Metric drop and sustain duration that forces a retrain |
| Rollback rule | Condition and observation period for automatic reversion |
| Maturity policy | That the window is set empirically in M2 by the Kaplan–Meier method, and the proportion targeted (95%) — the method is fixed now, the number is measured later |

Once committed, these are **not revised**. If a threshold later proves wrong,
the finding is documented in the summary and the original is left standing —
that record is worth more than a better-tuned number.

**Done when:** the commit hash is recorded, and it provably predates the first
entry in `model_registry` (AC-3).

---

## 4. Acceptance criteria

M0 is complete when **all** hold, verified in production:

| # | Criterion |
|---|---|
| A-1 | A named `revid` demonstrably went from unlabelled at ingestion to `mw-reverted` via a scheduled job, with no human action, and both timestamps are recorded |
| A-2 | The same revert is independently confirmed by the secondary tag path |
| A-3 | Re-running ingestion over an overlapping window inserts zero duplicate rows |
| A-4 | A two-hour deliberate outage is backfilled with no gap and no duplicates |
| A-5 | `bellwether_writer` is proven unable to `UPDATE` or `DELETE` rows in `labels`, on the production database |
| A-6 | VER-1, VER-2, VER-3 and VER-5 are answered with numbers in the summary; VER-4 is answered or explicitly deferred |
| A-7 | 24 hours of unattended scheduled runs with a complete `run_log` |
| A-8 | Public URLs serve live counts that change between visits without intervention |
| A-9 | `PREREGISTRATION.md` committed, including a power calculation using the measured base rate |
| A-10 | `Bellwether_M0_Summary.md` committed, recording what was built, what was measured, what surprised, and every non-obvious decision |

---

## 5. Kill criterion

**KC-1.** If, after 72 hours of operation, edits known to have been reverted are
not reliably discoverable through the primary path **and** the secondary path
also fails to reconstruct labels, then automatic ground truth does not exist and
**the project stops here.**

That is the correct outcome, not a failure — it would have cost one week instead
of two months. If it happens, the same architecture transfers to any feed whose
outcome becomes public after the prediction; the SRS's machinery is
domain-independent by design.

---

## 6. What M0 must not become

M0 has one job. Watch for these:

- Building features because they seem easy. Not until M2.
- Tuning the sampling rate before volume is measured. `R` is set in M1 from
  M0-T2's number.
- Writing pre-registration thresholds before the base rate exists. They would be
  guesses wearing the costume of a commitment.
- Starting the web application beyond the single stats page.
- Declaring M0 done on a green local run. Per the Definition of Done, production
  verification is the milestone.

---

## 7. Estimated effort

| Task | Effort |
|---|---|
| M0-T1 repo + CI | 0.5 day |
| M0-T2 ingest | 1 day |
| M0-T3 schema + grants + bootstrap | 1 day |
| M0-T4 labelling, both paths | 1.5 days |
| M0-T5 base rate | 0.5 day (mostly waiting) |
| M0-T6 Lift Wing probe | 0.5 day |
| M0-T7 workflows + outage test | 1 day |
| M0-T8 deployed skeleton | 1 day |
| M0-T9 pre-registration | 0.5 day |
| **Total** | **~7.5 working days**, plus 3 days of elapsed observation time that overlaps other tasks |
