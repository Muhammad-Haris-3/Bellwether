# Bellwether — Software Requirements Specification v1.0

**Project:** Bellwether — A Self-Maintaining Edit-Triage Service
**Author:** Muhammad Haris Khokhar
**Date:** 2026-08-13
**Status:** M0 in progress — VER-1 to VER-6 answered, see §4.3

---

## 1. Introduction

### 1.1 Purpose

This document specifies the requirements for **Bellwether**, a continuously
running classification service that scores English Wikipedia edits for the
probability that they will be reverted, **publishes each score before the
outcome exists**, grades itself automatically when the outcome arrives, and —
when its own accuracy decays — **retrains, shadow-tests and replaces its own
model without human instruction**, recording the evidence for every such
decision in a public, append-only log.

The deliverable is a **deployed software product** whose central claim is not
"my model is accurate" and not merely "here is how accurate it has been." It is:

> *Here is a system that noticed it was getting worse, built a replacement,
> tested the replacement against live reality, promoted or rejected it by a rule
> written before either model existed, and wrote down why — while nobody was
> watching.*

### 1.2 Scope

Bellwether polls the MediaWiki Action API on a fixed cadence, ingests a defined
sample of English Wikipedia edits, derives point-in-time-correct features from
its own accumulated history, scores each edit with the serving model, writes the
score to an append-only prediction register, later harvests the `mw-reverted`
change tag as ground truth, computes continuous out-of-sample metrics on matured
predictions only, monitors for performance decay and input drift, retrains and
shadow-evaluates challengers, promotes or rejects them under a pre-registered
rule, rolls back automatically on regression, and presents a live triage queue in
a multi-user web application whose reviewers' judgements feed back into training.

**In scope:** cursor-based incremental ingestion with gap-filling and
idempotency; a documented probability sampling frame with weight correction; a
point-in-time feature store with a knowability guard; right-censored label
maturity analysis; an append-only prediction register enforced by database
grant; continuous out-of-sample evaluation with calibration and segment
breakdowns; institutional benchmarking against Wikimedia Lift Wing's Revert Risk
model; drift and decay detection; automated retraining; shadow deployment;
pre-registered automated promotion, rejection and rollback; an immutable model
decision log; a multi-user web application with authentication, roles and
row-level security; a human review queue whose labels re-enter training; a
label-quality study; a read-only JSON API; and a written decision memo.

**Out of scope (v1.0):** real-time or sub-minute scoring latency (see §3.2 —
excluded by the zero-budget constraint, deliberately, with the consequence
stated); any wiki other than English Wikipedia; any namespace other than main;
deep learning or transformer models; article text content features requiring
full revision fetches; automated reverting or any write action against Wikipedia;
email or push notification delivery; mobile applications; paid infrastructure of
any kind; any claim of causal inference.

### 1.3 Definitions

| Term | Meaning |
|---|---|
| **Event** | One edit appearing in the MediaWiki `recentchanges` feed, identified by `revid` |
| **Revision id (`revid`)** | MediaWiki's immutable identifier for one revision of one page. The join key for the entire system |
| **`mw-reverted`** | A MediaWiki change tag applied to the edit **that was reverted**, not the edit that did the reverting. Applied asynchronously by the `revertedTagUpdate` job |
| **`mw-undo` / `mw-rollback` / `mw-manual-revert`** | Change tags applied to the **reverting** edit, by method. The secondary label path |
| **Label** | The binary outcome for an event: reverted (1) or not (0) |
| **Label maturity** | The elapsed time after an event beyond which its label is treated as final. Set empirically in M2, not guessed |
| **Matured prediction** | A prediction whose event is older than the maturity window. Only matured predictions may enter published metrics |
| **Right-censoring** | The condition that an unreverted recent edit may still be reverted later. The reason naive metrics are biased |
| **Run** | One execution of one scheduled job, identified by `run_id` |
| **Score time (`scored_at`)** | The instant a prediction was written. Immutable |
| **Prediction register** | The append-only table of every score ever issued. The project's evidential core |
| **Point-in-time correct** | Computed only from information the system had already ingested at the moment of the event being scored |
| **Knowability guard** | An automated assertion that raises if any feature depends on a row with `event_ts` later than the event it describes |
| **Sampling frame** | The explicit, deterministic rule defining which edits are ingested. Documented and weight-corrected |
| **Champion** | The model version currently serving the review queue |
| **Shadow** | A candidate model scoring every event in parallel, serving nothing, for the purpose of comparison |
| **Promotion** | Replacing the champion with a shadow, by the pre-registered rule only |
| **PR-AUC** | Area under the precision–recall curve. The primary metric, chosen because the positive class is rare |
| **Precision@k** | The proportion of the top *k* ranked edits that were genuinely reverted. The metric a reviewer actually experiences |
| **Brier score / calibration** | Whether a stated probability of 0.3 corresponds to a 30% observed revert rate |
| **PSI** | Population Stability Index — the input-drift statistic |
| **Proxy label** | "Was reverted" used as a stand-in for "was a bad edit." A noisy proxy, whose noise is measured in M7 rather than assumed away |

### 1.4 Intended audience

Primarily hiring managers and technical interviewers assessing analytical and
engineering capability. Sections 2, 3 and the decision memo are written to be
read by a non-technical reader. Sections 6–12 are written so a technical reader
can reproduce every published number, audit every accuracy claim, and verify
that no model promotion decision could have been made after seeing its result.

---

## 2. Business context and problem statement

### 2.1 Context

English Wikipedia accepts edits from anyone with no prior approval. A small
volunteer patrol community inspects those edits for vandalism, spam, and subtle
factual damage. The volume of edits vastly exceeds what that community can read,
so attention is allocated close to arrival order — meaning most reviewer
attention is spent on edits that were fine.

This is the same operational shape as fraud alert triage, marketplace listing
review, and content moderation: a high-volume stream, a rare adverse class, a
scarce human review budget, and a ranking problem in between.

### 2.2 Problem statement

> A ranking model deployed against a live stream begins to decay the moment it
> ships, because the behaviour it ranks is generated by people whose behaviour
> changes. Almost every deployed model is evaluated once, at training time,
> against a held-out sample of the past — and thereafter is trusted. Nobody
> measures whether it is still right, nobody defines in advance what "no longer
> right" means, and the decision to replace it is made by a human who has
> already seen which replacement won.

### 2.3 Primary business question

**Can a system detect its own decay, replace its own model, and be trusted to
have done so honestly?**

Decomposed into answerable sub-questions:

| # | Question | Method |
|---|---|---|
| BQ-1 | What fraction of edits are reverted, and how does that vary by editor class, namespace activity, time of day and day of week? | Descriptive, weight-corrected |
| BQ-2 | How long does it take for a revert to occur, and how much of the eventual total has landed by T+1h, T+6h, T+24h, T+48h, T+7d? | Survival analysis under right-censoring |
| BQ-3 | Can point-in-time features available at edit time rank revert risk better than arrival order and than simple heuristics (anon-only, size-delta)? | Rolling-origin backtest, PR-AUC |
| BQ-4 | Are the model's stated probabilities calibrated, and does the case-control sampling correction restore calibration? | Reliability curves, Brier decomposition |
| BQ-5 | How does the model compare to Wikimedia's own Lift Wing Revert Risk model on the same events? | Paired comparison on identical matured events |
| BQ-6 | Does accuracy decay measurably over time, and do input-drift statistics anticipate that decay or lag it? | Rolling metrics vs. PSI, lead-lag analysis |
| BQ-7 | When a challenger beats the champion, is the difference real or noise? | Pre-registered paired bootstrap / DeLong, with a pre-registered minimum sample |
| BQ-8 | How good a proxy is "was reverted" for "was a bad edit," as judged by human reviewers? | Agreement analysis, Cohen's κ, error decomposition |

### 2.4 Success criteria

The project succeeds if a visitor can, without contacting the author:

1. See the current review queue and the record of how accurate previous
   rankings at the same threshold turned out to be.
2. Verify that no published score was edited after its outcome became known.
3. See every model promotion, rejection and rollback the system has made, the
   evidence behind each, and the rule — committed to git before any model
   existed — that decided it.
4. Identify at least one occasion on which the system rejected a challenger
   that looked better, or rolled back a promotion, and understand why.
5. Read a two-page memo explaining what the system found, with no technical
   background.

### 2.5 What would make this project a failure

Stated explicitly so it can be checked against:

- A live dashboard that updates on a schedule but whose model never changes
  itself. This is the project's primary failure mode and would reduce it to a
  restatement of prior work.
- Metrics computed over unmatured predictions, which would be optimistically
  biased and invalidate every accuracy claim.
- A promotion rule written or adjusted after seeing challenger results.

---

## 3. Feasibility study

### 3.1 Technical feasibility

| Requirement | Verified position (2026-08-13) |
|---|---|
| Event feed | MediaWiki Action API `list=recentchanges`; `rcprop` supports `ids`, `timestamp`, `user`, `userid`, `comment`, `flags`, `sizes`, `tags`, `patrolled`; `rclimit` 1–500; enumeration by `rcstart`/`rcdir=newer` with `rccontinue` paging. Keyless |
| Ground truth | `mw-reverted` is applied **to the reverted edit**, via the deferred `revertedTagUpdate` job. Retrievable per-revision via `prop=revisions&rvprop=tags` |
| Secondary ground truth | `mw-undo`, `mw-rollback`, `mw-manual-revert` tag the **reverting** edit and appear in the live feed; manual-revert detection radius defaults to `$wgManualRevertSearchRadius` = 15 revisions |
| Rate limit | 200 requests/minute for unauthenticated clients sending a meaningful `User-Agent` with contact details (10/min without one). At the planned cadence the system needs well under 1% of this |
| Benchmark | Wikimedia Lift Wing hosts a **Revert Risk** model family (language-agnostic and multilingual), which replaced the deprecated ORES `damaging`/`goodfaith` models. Exact request shape **unverified — VER-4 in §4.3** |
| Compute | GitHub Actions scheduled workflows, unlimited minutes on public repositories |
| Serving | Render free web service, Vercel, Neon free PostgreSQL |

### 3.2 Constraint: no always-on process

The project has a hard **zero-cost** constraint. No free tier provides a
persistent process able to hold a long-lived stream connection reliably.

**Consequence, stated rather than hidden:** Bellwether is a *near-real-time*
system, not a streaming one. Events are scored within one polling interval
(target ≤ 10 minutes) rather than within milliseconds.

**Why this does not damage the project:** `recentchanges` enumeration is
cursor-based, so polling loses no events — only latency. Every property the
project's claim rests on (self-grading, decay detection, retraining, shadow
evaluation, automated promotion, rollback) is unaffected, because all of those
operate on a batch cadence in any architecture, including paid ones.

**What is genuinely forfeited:** demonstrated experience with streaming
transport, backpressure and exactly-once delivery semantics. This is recorded
here so that it is never implied otherwise.

### 3.3 Constraint: storage

Free-tier PostgreSQL storage cannot hold every English Wikipedia edit at
indefinite retention. This is treated as a **design input**, not an obstacle:
it forces an explicit probability sampling frame (§6.3) and a retention policy
(§6.5). The resulting weight correction is an analytical asset, not a
compromise.

### 3.4 Principal risks to validity

Two threats can silently invalidate every number the project publishes. They are
named here because the architecture exists mainly to defeat them.

**Threat 1 — Lookahead leakage through editor features.**
The obvious feature "how many edits has this user made" is fatal if read from
the API at training time, because the API returns *today's* count, not the count
at the moment of the edit. A model trained on it would appear excellent and be
worthless. **Mitigation:** every editor-derived feature is computed exclusively
from events Bellwether has already ingested, in event order, materialised into a
running `editor_state` table. A knowability guard (FR-14) asserts that no
feature row draws on an event with a later timestamp than its subject, and
raises rather than warns.

**Threat 2 — Immature labels.**
An edit scored an hour ago and not yet reverted is not a negative; it is
censored. Computing metrics over recent predictions inflates apparent
performance because reverts have not had time to arrive. **Mitigation:** the
maturity window is estimated empirically in M2 from the revert-latency survival
curve, and no prediction may enter a published metric before it matures
(FR-19). Immature predictions are visible in the app but excluded from every
accuracy claim, and the two are never pooled.

### 3.5 Kill criteria

The project is abandoned or redesigned, rather than continued, if:

- **KC-1:** After M0, the `mw-reverted` tag cannot be reliably retrieved for
  previously ingested revisions within 72 hours, **and** the secondary
  revert-tag path (§7.2) also fails to reconstruct labels. Without automatic
  ground truth there is no project.
- **KC-2:** After M2, no feature set computable from ingested history and free
  of leakage outperforms the trivial "anonymous editor" heuristic on PR-AUC by a
  meaningful margin. A system that maintains a model no better than one `if`
  statement is not worth maintaining.

---

## 4. SDLC methodology

### 4.1 Walking skeleton first

M0 delivers the thinnest possible end-to-end slice — ingest one window, score
nothing, harvest one label, show one number in a deployed browser — before any
modelling work begins. The purpose is to falsify KC-1 in days rather than
discover it in month two.

### 4.2 Definition of Done (applies to every milestone)

A milestone is not complete until **all** of the following hold:

1. Code merged to `main`, CI green (lint, typecheck, unit tests, dbt/SQL tests).
2. Deployed to production and verified **in production**, not locally.
3. Every requirement in the milestone's scope demonstrably exercised against
   the deployed system.
4. A `Bellwether_M<n>_Summary.md` committed, recording what was built, what was
   measured, what surprised, and every non-obvious decision with its reasoning.
5. Any figure quoted in documentation regenerated from the live system, not
   copied forward.

A green local run and a successful push are **not** completion.

### 4.3 Verification items carried into M0

Items the specification depends on that are not yet confirmed against the live
API. Each must be resolved in M0 before dependent work starts.

| # | Item | Status |
|---|---|---|
| VER-1 | Whether `mw-reverted` becomes visible on a previously ingested revision | **Answered 2026-08-13.** Yes. 128 of 4,000 edits in a 5–6 hour old window carried the tag, alongside 87 `mw-undo` on the reverting side |
| VER-2 | Actual volume of main-namespace non-bot enwiki edits | **Preliminary 2026-08-13:** ~62 edits/minute, ≈ 90k/day. Requires a full 24-hour cycle before `R` is set (M0-T2) |
| VER-3 | Observed base revert rate, split by editor class | **Preliminary 2026-08-13** at ~5h maturity: 19.1% logged-out (n=392), 1.5% registered (n=3,608), 3.2% overall. Lower bounds — reverts continue to arrive. Final figure at T+48h in M0-T5 |
| VER-4 | Lift Wing Revert Risk endpoint, body, auth and limits | **Answered 2026-08-13.** `POST api.wikimedia.org/service/lw/inference/v1/models/revertrisk-language-agnostic:predict`, body `{"lang":"en","rev_id":N}`, **no authentication**. `revertrisk-multilingual` also live. Returns a calibrated probability. See §6.4 |
| VER-5 | Whether `recentchanges` re-polling returns tags applied after the edit | **Answered 2026-08-13.** Yes — the tags above arrived via the recentchanges feed, not per-revision lookups. This is a **cheaper primary harvest path than planned**: 500 revisions per request rather than 50 (§7.2 note) |
| VER-6 | *(new)* Whether logged-out editors are identifiable | **Answered 2026-08-13.** Not by `anon`, which is never set. Temporary accounts carry a `temp` flag. See §6.3 |

---

## 5. Stakeholders and user characteristics

| Stakeholder | Interest | Technical level |
|---|---|---|
| Hiring manager / interviewer | Evidence of production ML lifecycle capability and honest measurement | Mixed |
| Reviewer (app user, `reviewer` role) | A ranked queue that saves reading time; ability to mark items | Non-technical |
| Administrator (`admin` role) | Model timeline, drift state, run health, ability to freeze automation | Technical |
| Anonymous visitor (`viewer`) | Public metrics, model timeline, methodology | Non-technical |

Bellwether performs **no write action of any kind against Wikipedia**. It reads
public data and presents rankings. This is a stated constraint, not an oversight.

---

## 6. Data specification

### 6.1 Sources

| # | Source | Endpoint | Auth | Used for |
|---|---|---|---|---|
| DS-1 | MediaWiki Action API — recent changes | `en.wikipedia.org/w/api.php?action=query&list=recentchanges` | none | Event feed |
| DS-2 | MediaWiki Action API — revisions | `…&action=query&prop=revisions&revids=…&rvprop=ids\|timestamp\|tags` | none | Label harvest (`mw-reverted`) |
| DS-3 | Wikimedia Lift Wing — Revert Risk | see VER-4 | TBC | Institutional benchmark (M4) |

All requests carry a `User-Agent` naming the project, its repository URL and a
contact address, per Wikimedia policy, to qualify for the 200 req/min tier.

### 6.2 Event fields captured

From DS-1 with `rcprop=ids|timestamp|title|user|userid|comment|flags|sizes|tags|patrolled`:
`revid`, `old_revid`, `rcid`, `timestamp`, `ns`, `title`, `user`, `userid`,
`anon` flag, `bot` flag, `minor` flag, `patrolled` flag, `comment`, `oldlen`,
`newlen`, `tags[]`.

**Design note.** The entire v1 feature set is derivable from these fields plus
Bellwether's own accumulated history. No per-event API call is required to score
an edit. This keeps request volume near-constant regardless of sampling rate,
and — more importantly — makes point-in-time correctness structurally easy,
because there is no live lookup that could return a present-day value.

### 6.3 Sampling frame

Storage does not permit full ingestion (§3.3). The frame is therefore explicit,
deterministic and reproducible:

```
INCLUDE  namespace = 0 (main)
AND      type = edit
AND      bot flag = false
AND      ( anon = true OR temp = true               -- stratum A: logged out,
                                                    -- sampled at 100%
           OR hash(revid) mod 100 < R )             -- stratum B: registered,
                                                    -- sampled at R%
```

> **Revised 2026-08-13 during M0, on measurement.** Stratum A was originally
> defined as `anon = true` alone. English Wikipedia has since adopted
> **temporary accounts**: a logged-out editor is no longer identified by IP but
> is issued an auto-created account (e.g. `~2026-44334-20`) carrying a `temp`
> flag, and `anon` never appears. Measured over 2,498 live main-namespace
> edits, `anon` was true for **zero** of them. The original frame would have
> placed 100% of traffic in stratum B and sampled away most of the positive
> class before training began. The dividing line is *logged out* versus *logged
> in*, and the API's `temp` flag is used rather than a pattern match on the
> username.

- `R` is set in M1 from the volume measured in M0-T2, targeting a steady-state
  row budget defined in §6.5.
- Sampling is by deterministic hash of `revid`, not by random draw, so the frame
  is reproducible and auditable, and re-running ingestion selects the identical
  set.
- **This is case-control sampling.** Stratum A carries a substantially higher
  base revert rate than stratum B, so the sampled positive rate exceeds the
  population rate. Raw model probabilities will therefore be miscalibrated
  against the population. Measured at ~5 hours of maturity: **19.1%** in
  stratum A against **1.5%** in stratum B — a 13× gap, which is both the
  justification for the stratification and the reason the correction below is
  not optional.

**FR-6 requires the correction:** every published probability is corrected to
the population prior via a sampling-weight offset applied to the model intercept,
and calibration is reported both before and after correction, on population
weights. Population-weighted estimates accompany every descriptive statistic.

### 6.4 The benchmark is strong, and that changes what this project claims

Measured 2026-08-13 on 16 revisions drawn at random from ingested data, 8
reverted and 8 not:

| | n | Median score | Range |
|---|---|---|---|
| Reverted | 8 | 0.928 | 0.729 – 0.979 |
| Not reverted | 8 | 0.177 | 0.079 – 0.636 |

Complete separation — the lowest-scoring revert (0.73) sits above the
highest-scoring survivor (0.64). Sixteen points is not an AUC and this is not a
measurement; it is a signal about what M4 will find.

**The honest consequence.** Wikimedia's production model is very good, and
Bellwether's tabular baseline is unlikely to beat it. That is recorded here,
before any model exists, so that the project is not quietly redefined later to
avoid admitting it.

It does not damage the deliverable, because ranking quality was never the
claim. What is not published by anyone — including Wikimedia — is a
continuously maintained, out-of-sample record of how a deployed scorer has
actually performed, alongside a system that detects its own decay and replaces
itself under a rule fixed in advance. **Lift Wing is the thing to be measured
against, not the thing to be beaten**, and a project that measures itself
honestly against a superior benchmark is worth more than one that picks a
weaker opponent.

Consequences for later milestones:

- FR-26's paired comparison is now expected to favour Lift Wing, and the
  pre-registration must not be written to avoid that outcome.
- KC-2 stands unchanged: the kill criterion is beating the trivial
  logged-out heuristic, not beating Wikimedia.
- M5's promotion machinery is unaffected — champion and challenger are both
  Bellwether's own models, and the point is the mechanism, not the winner.

### 6.5 Label sources

| Path | Tag | Applied to | Timing | Role |
|---|---|---|---|---|
| Primary | `mw-reverted` | the reverted edit | deferred job queue | Authoritative label |
| Secondary | `mw-undo`, `mw-rollback`, `mw-manual-revert` | the reverting edit | at edit time, visible in live feed | Cross-check and fallback |

The secondary path is implemented even if the primary works, because agreement
between two independently derived labels is itself evidence, and because
`$wgManualRevertSearchRadius` (15) bounds what manual-revert detection can see.
Disagreement rate between paths is reported (FR-20), never silently resolved.

### 6.6 Retention

| Data | Retention | Reason |
|---|---|---|
| `rc_events` raw rows | 120 days, then rolled up to daily aggregates and deleted | Storage ceiling |
| `predictions`, `labels`, `model_decisions`, `metrics` | **indefinite** | These are the evidence; deleting them would destroy the project's claim |
| `features` | 120 days, regenerable from `rc_events` while those exist | Space |
| `run_log` | indefinite | Auditability of gaps |

Retention runs as a scheduled job with a dry-run mode and logs every deletion
count to `run_log`.

---

## 7. Functional requirements

### 7.1 Ingestion and orchestration

| # | Requirement |
|---|---|
| FR-1 | The system shall poll DS-1 on a fixed cadence (target 10 minutes) via GitHub Actions scheduled workflows |
| FR-2 | Ingestion shall be **cursor-based**, resuming from the last successfully committed event timestamp, so that a delayed, skipped or failed run causes latency but never data loss |
| FR-3 | Ingestion shall be **idempotent**: re-running any window shall produce no duplicate rows, enforced by a unique constraint on `revid`, not by application logic alone |
| FR-4 | The system shall detect and automatically backfill gaps between the cursor and the current time, in bounded pages, respecting the 200 req/min limit |
| FR-5 | Every job execution shall write a `run_log` row recording job name, `run_id`, window, rows affected, API calls made, duration and outcome |
| FR-6 | Ingestion shall apply the §6.3 sampling frame deterministically and record the sampling weight on every row |

### 7.2 Labelling

| # | Requirement |
|---|---|
| FR-7 | A scheduled job shall harvest `mw-reverted` for previously ingested revisions, in batches, prioritising events near the maturity boundary |

> **Note added 2026-08-13 (VER-5).** `recentchanges` re-polling returns tags
> applied *after* the edit, so a window can be re-swept 500 revisions at a time
> instead of 50 per `prop=revisions` call — a tenfold reduction in requests
> against a free public service. DS-2 remains implemented as the authoritative
> path, because recentchanges rows are purged on a retention horizon the wiki
> controls and we do not, and the 7-day checkpoint sits close enough to that
> horizon to matter. Which path is primary is decided in M1 on measured cost.
| FR-8 | A second, independent job shall derive labels from `mw-undo`/`mw-rollback`/`mw-manual-revert` events observed in the live feed, mapping the reverting edit back to the revisions it reverted |
| FR-9 | Labels shall record `label`, `label_source`, `first_observed_at`, `revert_latency_seconds` and, where known, `revert_revid` |
| FR-10 | Labels shall be **append-only with respect to the first observation**: a later re-observation may add a row but shall never overwrite the original observation timestamp |
| FR-11 | The system shall report the disagreement rate between primary and secondary label paths as a published data-quality metric |

### 7.3 Features and modelling

| # | Requirement |
|---|---|
| FR-12 | The system shall maintain an `editor_state` table of running per-editor aggregates, updated strictly in event order |
| FR-13 | Every feature shall be computable from events with `event_ts` strictly less than the subject event's `event_ts` |
| FR-14 | A **knowability guard** shall assert FR-13 for every feature build and shall **raise**, failing the job, on violation — never warn |
| FR-15 | Feature vectors shall be persisted with a `feature_hash` so that any historical prediction can be reproduced exactly |
| FR-16 | Model training shall use rolling-origin evaluation, never a random split, because the data is temporally ordered |
| FR-17 | Each trained model shall be registered with version, artifact hash, training window, hyperparameters, feature list and offline metrics |

### 7.4 Scoring and the register

| # | Requirement |
|---|---|
| FR-18 | The champion model shall score every newly ingested event, writing to the append-only `predictions` register with `scored_at`, `model_version` and `role = 'champion'` |
| FR-19 | The `predictions` table shall be protected by database **grant**: the application role shall hold no `UPDATE` or `DELETE` privilege on it. The guarantee shall rest on a permission, not a convention |
| FR-20 | A `CHECK` constraint shall enforce `scored_at >= event_ts`, making a backdated score structurally impossible |
| FR-21 | Any shadow model shall score the same events with `role = 'shadow'`, serving nothing |

### 7.5 Evaluation

| # | Requirement |
|---|---|
| FR-22 | Metrics shall be computed **only** over matured predictions, per §1.3, and the maturity window shall be a recorded parameter of every metric row |
| FR-23 | The system shall compute rolling PR-AUC, precision@k, recall@k, Brier score and calibration error, per model version, over fixed windows |
| FR-24 | Metrics shall be broken down by segment: editor class (anon/registered), edit size band, hour of day, and page activity band |
| FR-25 | Backtest metrics and live out-of-sample metrics shall be stored and displayed in **separate columns and never pooled** |
| FR-26 | The system shall score the same matured events with the Lift Wing Revert Risk model and report a paired comparison |

### 7.6 Self-maintenance — the core of the project

| # | Requirement |
|---|---|
| FR-27 | A **decay detector** shall fire when the champion's rolling primary metric falls below its registered baseline by more than the pre-registered margin, sustained over a pre-registered number of consecutive windows |
| FR-28 | A **drift detector** shall fire when PSI on any monitored feature, or on the score distribution, exceeds the pre-registered threshold |
| FR-29 | Either detector, or a scheduled floor interval, shall trigger automatic retraining on the most recent fully matured labelled window, without human instruction |
| FR-30 | A newly trained candidate shall enter **shadow mode**, scoring live events alongside the champion, and shall not serve |
| FR-31 | A shadow shall be eligible for promotion only after accumulating **both** a pre-registered minimum elapsed duration **and** a pre-registered minimum number of matured labelled events, the latter derived from a power calculation committed before any model exists |
| FR-32 | Promotion shall occur **only** if the pre-registered rule is satisfied: superiority on the primary metric by at least the registered margin, significance under the registered paired test, no degradation in calibration beyond tolerance, and no segment regression beyond tolerance |
| FR-33 | Promotion, rejection and rollback shall be executed **automatically**, with no human approval step in the path |
| FR-34 | An **automatic rollback** shall restore the previous champion if a newly promoted model's rolling metric falls below the pre-promotion registered level within the pre-registered observation period |
| FR-35 | Every promotion, rejection and rollback shall append an immutable `model_decisions` row containing the rule version, every input value, the test statistic, the outcome, and the reason — sufficient for a reader to recompute the decision |
| FR-36 | Model artifacts and their metric cards shall be committed to the public repository at decision time, so that the decision history is independently verifiable from git alone, by someone with no database access |
| FR-37 | An administrator shall be able to **freeze** automation (halting promotion) but shall not be able to alter, delete or backdate any past decision |

### 7.7 Application

| # | Requirement |
|---|---|
| FR-38 | ~~The web application shall provide email-based authentication with sessions stored server-side~~ **Amended 2026-08-14, see below.** The application shall provide credential-based authentication with sessions stored server-side. Accounts are created by an administrator; there is no self-service sign-up and no email is sent |

> **Amendment to FR-38 — 2026-08-14, before M6 was built.**
>
> Email-based sign-in requires something that sends email. A transactional
> provider or Gmail SMTP would work, and both put the project's zero-cost
> guarantee (NFR-1) at the mercy of a free tier that can be withdrawn or
> throttled, plus an API key in a deployment environment.
>
> The requirement is therefore narrowed rather than dropped: accounts are
> issued by an administrator, with a strong generated password shown once — the
> same pattern `scripts/bootstrap_database.py` already uses for database roles.
> Sessions remain server-side, which was the part of FR-38 that carried the
> security property.
>
> **What is lost:** no self-service sign-up, no password reset by email, and no
> way for a stranger to obtain an account. For a system with a handful of
> reviewers that is acceptable; for a real deployment it would not be, and this
> note is here so nobody has to reconstruct why.
>
> Recorded before implementation, not after. FR-39 to FR-46 are unchanged.
| FR-39 | The application shall implement three roles — `viewer`, `reviewer`, `admin` — enforced at the database layer via PostgreSQL row-level security, not in application code alone |
| FR-40 | ~~Authenticated reviewers shall see a live triage queue of recent events ranked by champion score~~ **Amended 2026-08-14, see below.** Authenticated reviewers shall see a live triage queue whose CONTENTS are selected by champion score, refreshing without a page reload. Display order within a page is not the ranking, and the model's score is withheld until a verdict is recorded |

> **Amendment to FR-40 — 2026-08-14, before M7 was built.**
>
> M7 adds a slice of the queue drawn at random rather than by rank, because a
> queue ranked by the model, labelled by a human, and fed back into the model
> teaches it only about edits it already flagged. Its false negatives — the
> errors that matter — are invisible to that loop.
>
> The random slice only works as a control if a reviewer cannot tell which rows
> it contains. Sorted by score, every randomly drawn row sinks to the bottom;
> shown with its score, it is obviously different in kind. So the page is
> shuffled and the score is withheld until after the verdict is recorded.
>
> **What is lost:** the reviewer no longer works strictly highest-risk-first
> within a page. Triage moves from the row to the batch — the page is still
> selected by score, so it is still mostly high-risk work.
>
> **What is gained beyond the control:** a judgement formed without seeing the
> model's opinion is a judgement about the edit rather than agreement with a
> number, which is the only kind that can answer BQ-8.
>
> Recorded before implementation. FR-41 to FR-46 are unchanged.
| FR-41 | The queue shall clearly distinguish **immature** items (outcome not yet final) from matured ones, and shall never present an unmatured item as evidence of accuracy |
| FR-42 | A reviewer shall be able to mark an item, recording a human label with reviewer identity, timestamp and confidence |
| FR-43 | All role-sensitive and state-changing actions shall append to an immutable `audit_log` |
| FR-44 | A public model timeline page shall show every promotion, rejection and rollback with its evidence, without authentication |
| FR-45 | A public metrics page shall show current and historical out-of-sample performance, with the maturity window and sample size stated on every figure |
| FR-46 | A read-only JSON API shall expose current metrics, model decisions and the queue |

### 7.8 Human labels and label quality

| # | Requirement |
|---|---|
| FR-47 | Human labels shall be stored separately from automatic labels, with provenance, and shall never be silently merged into automatic-label metrics |
| FR-48 | Human labels shall enter retraining as an additional weighted signal, with the weight recorded in the model registry entry |
| FR-49 | The system shall report agreement between human judgement and the revert proxy (Cohen's κ, confusion matrix), quantifying how good a proxy "was reverted" is for "was a bad edit" |

### 7.9 Communication

| # | Requirement |
|---|---|
| FR-50 | A `PREREGISTRATION.md` shall be committed **at M0, before any model exists**, fixing the primary metric, margin, minimum sample, statistical test, segment tolerances, rollback rule and drift thresholds |
| FR-51 | A `METHODS.md` shall document the sampling frame, weight correction, maturity estimation and every statistical procedure |
| FR-52 | A `DECISION_MEMO.md` of at most two pages shall state what the system found, readable with no technical background |

---

## 8. Non-functional requirements

| # | Requirement |
|---|---|
| NFR-1 | **Zero infrastructure cost.** No component may require payment at any usage level the project will reach |
| NFR-2 | Total API request volume shall remain below 20% of the 200 req/min limit at steady state |
| NFR-3 | All outbound requests shall carry a compliant `User-Agent` with contact details |
| NFR-4 | Database storage shall remain below 80% of the free-tier ceiling at steady state, enforced by the retention job and monitored |
| NFR-5 | Any scheduled job shall complete within 10 minutes, so a run never overlaps its successor |
| NFR-6 | Jobs shall be safe to run concurrently by accident, via advisory locking |
| NFR-7 | The API shall tolerate cold starts; the frontend shall show a determinate loading state rather than appearing broken |
| NFR-8 | No credential with write access to `predictions` or `model_decisions` shall ever be held by the serving API container |
| NFR-9 | No secret shall be exposed in any API response, including health and diagnostic endpoints |
| NFR-10 | Every published figure shall carry its sample size and its maturity window |
| NFR-11 | The system shall degrade honestly: if a job has not run, the app shall state the staleness rather than serve a stale figure silently |
| NFR-12 | Accessibility: keyboard-operable queue, WCAG AA contrast, screen-reader labels on all controls |

### 8.1 How NFR-8 is satisfied

Following the credential separation established in prior work: the Render
container holds a **read-only** database role. Writes to the register occur only
from GitHub Actions jobs, which hold the writer credential and are themselves
granted `INSERT` but not `UPDATE`/`DELETE` on evidential tables. Neither the app
nor the pipeline is technically able to rewrite history; the guarantee does not
depend on either choosing not to.

---

## 9. Architecture

### 9.1 Layered design

```
MediaWiki Action API (recentchanges, revisions)   ·   Lift Wing Revert Risk
        │  keyless, 200 req/min, UA-identified
        ▼
GitHub Actions — ingest · label · features · metrics · drift · train · decide
        │  cursor-based · idempotent · gap-filling · run-logged · advisory-locked
        ▼
landing    rc_events            append-only, sampled, weighted
        ▼
derived    editor_state · features           point-in-time, knowability-guarded
        ▼
register   predictions          APPEND-ONLY BY GRANT   ← evidential core
        │        ▲
        │        └── champion + shadow, same events, same instant
        ▼
outcome    labels · metrics · drift_checks   matured only
        ▼
decide     model_decisions      APPEND-ONLY   ← promotion / rejection / rollback
        │                        + artifact & metric card committed to git
        ▼
serve      FastAPI (read-only role, Render)  →  Next.js (Vercel)
                                                  queue · timeline · metrics
                                                  auth · roles · RLS · audit
```

### 9.2 Technology decisions

| Decision | Choice | Rejected | Reason |
|---|---|---|---|
| Scheduler | GitHub Actions | Render cron, Fly, VPS | Only option that is unlimited and free on public repos (NFR-1) |
| Transport | Polling `recentchanges` | SSE `stream.wikimedia.org` | SSE needs an always-on process (§3.2). Cursor polling loses no events |
| Database | Neon PostgreSQL | SQLite in repo | Needs concurrent writers, RLS, and grant-level append-only enforcement |
| Model | Gradient-boosted trees (scikit-learn) | Deep learning | Tabular, small, must train inside a 10-minute free CI job, and must be explainable to a reviewer |
| Model registry | Git-committed artifacts + metric cards | Database blobs only | Public git history makes the decision log independently auditable without DB access (FR-36) |
| Live updates | Client polling / SSE from Render | WebSockets | Render free tier sleeps; polling degrades gracefully, sockets do not |

### 9.3 Free-tier constraints treated as design inputs

| Constraint | Design response | Analytical benefit |
|---|---|---|
| No always-on process | Cursor polling | None — cost recorded honestly in §3.2 |
| Storage ceiling | Explicit sampling frame + retention | Forces a documented frame and weight correction (§6.3) |
| 10-min CI job limit | Bounded pages, incremental training | Forces idempotency and restartability |
| Render sleeps | Read-only API, cold-start-tolerant UI | Forces honest staleness reporting (NFR-11) |

---

## 10. Conceptual data model

| Table | Purpose | Mutability |
|---|---|---|
| `run_log` | Every job execution | insert-only |
| `rc_events` | Sampled raw events with sampling weight | insert-only, 120-day retention |
| `editor_state` | Running point-in-time editor aggregates | updatable (derived, regenerable) |
| `features` | Persisted feature vectors + `feature_hash` | insert-only, regenerable |
| `predictions` | Every score ever issued | **APPEND-ONLY BY GRANT** |
| `labels` | Outcomes, per source, with latency | insert-only |
| `metrics` | Rolling metrics per model/window/segment | insert-only |
| `drift_checks` | PSI and decay evaluations | insert-only |
| `model_registry` | Versions, artifacts, training windows | insert-only |
| `model_decisions` | Promotions, rejections, rollbacks + evidence | **APPEND-ONLY BY GRANT** |
| `users`, `memberships` | Accounts and roles | updatable |
| `reviews` | Human labels with provenance | insert-only |
| `audit_log` | All state-changing actions | insert-only |

---

## 11. Analysis plan

| Stage | Method |
|---|---|
| Label maturity | Kaplan–Meier estimate of revert latency under right-censoring; maturity window set at the time by which a pre-registered proportion (target 95%) of eventual reverts have landed |
| Base rates | Weight-corrected revert rates by stratum and segment |
| Baselines | Arrival order; anon-only heuristic; size-delta heuristic. A model must beat all three or KC-2 applies |
| Model | Gradient-boosted trees with class weighting; no resampling, so probabilities stay interpretable |
| Calibration | Reliability curves and Brier decomposition, before and after the sampling-prior correction |
| Comparison | Paired bootstrap on matched matured events for PR-AUC; DeLong for ROC-AUC as a secondary check |
| Power | Minimum matured sample for promotion derived before any model exists, from the observed base rate and the registered margin |
| Drift | PSI per feature and on the score distribution; lead-lag analysis against realised metric decay |
| Label quality | Cohen's κ between human labels and the revert proxy; decomposition of disagreements |

Every one of these is fixed in `PREREGISTRATION.md` at M0.

---

## 12. Milestone plan

| # | Milestone | Delivers |
|---|---|---|
| **M0** | **Label loop proof + walking skeleton** | Falsifies KC-1. Ingest → wait → harvest label → show it in a deployed browser. Pre-registration committed |
| M1 | Durable ingestion | Cadence, cursor, gap-fill, idempotency, sampling frame, retention, run log, data-quality checks |
| M2 | Features + maturity | `editor_state`, knowability guard, revert-latency survival analysis, maturity window, offline baseline vs. three heuristics |
| M3 | Scoring + register | Champion scoring, append-only register enforced by grant, feature hashing, reproducibility |
| M4 | Continuous evaluation | Rolling metrics on matured predictions, segments, calibration, Lift Wing benchmark |
| **M5** | **Self-maintenance** | Decay + drift detection → automatic retrain → shadow → pre-registered promotion → auto-rollback → immutable decision log. **Defensible stopping point** |
| M6 | Application | Auth, roles, RLS, live queue, audit log, public timeline and metrics pages |
| M7 | Human feedback loop | Reviewer labels feeding weighted retraining; label-quality study (κ vs. proxy) |
| M8 | Communication | Alerting, decision memo, methods doc, README |

**M0–M5 is a complete and defensible project.** M6–M8 is what makes it a
product rather than a pipeline.

---

## 13. Risks and mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R-1 | `mw-reverted` not retrievable as assumed | Low | Fatal | M0 tests it first; secondary tag path implemented as fallback; KC-1 |
| R-2 | Lookahead leakage via editor features | High if unguarded | Fatal | Knowability guard raises, not warns (FR-14) |
| R-3 | Metrics computed on immature labels | High if unguarded | Fatal | Maturity window enforced in the metric query itself (FR-22) |
| R-4 | Model no better than an `if` statement | Medium | High | KC-2 checked at M2, before building the self-maintenance machinery |
| R-5 | GitHub Actions cron delayed or skipped | High | Low | Cursor-based ingestion converts a missed run into latency, not loss |
| R-6 | Scheduled workflows disabled after 60 days of repo inactivity | Medium | Medium | Pipeline commits artifacts and seals, keeping the repo active; monitored via staleness check |
| R-7 | Storage ceiling reached | Medium | Medium | Sampling rate `R` is a tunable parameter; retention job monitored against NFR-4 |
| R-8 | Model never decays, so promotion machinery never demonstrates itself | Medium | **High** | Deliberate forced-decay exercise at M5: train a candidate on a deliberately stale window and verify the system rejects it, and inject a synthetic degradation to verify rollback fires. Evidence of the mechanism cannot wait on nature |
| R-9 | Rate limiting or blocking by Wikimedia | Low | High | Compliant `User-Agent`, volume at <20% of limit, exponential backoff, honour `Retry-After` |
| R-10 | Scope creep past M5 before M5 is done | High | Medium | M6–M8 not started until M5 meets Definition of Done |

**R-8 deserves emphasis.** The single most likely way this project ends up
unimpressive is that everything works and nothing ever changes, leaving an empty
model timeline. The forced-decay exercise is therefore a **requirement of M5**,
not an optional extra: the system must be shown rejecting a bad challenger and
rolling back a bad promotion, on evidence, in production.

---

## 14. Acceptance criteria

The project is accepted when a reader with no access to the author can:

| # | Criterion |
|---|---|
| AC-1 | Observe a score issued for an edit, and later observe that same edit's automatically harvested outcome, without either being editable |
| AC-2 | Confirm from database grants that the serving role cannot modify the prediction register or the decision log |
| AC-3 | Read `PREREGISTRATION.md` in git history and confirm its commit predates the first entry in `model_registry` |
| AC-4 | See out-of-sample metrics computed only over matured predictions, with maturity window and sample size stated |
| AC-5 | See at least one automatic promotion **and** at least one automatic rejection or rollback in the model timeline, each with recomputable evidence |
| AC-6 | Verify a past promotion independently from git-committed artifacts and metric cards alone |
| AC-7 | Confirm backtest and live metrics are never pooled |
| AC-8 | See the measured agreement between human judgement and the revert proxy |
| AC-9 | Read a two-page memo and understand what the system does and found, with no technical background |

---

## 15. Document control

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-08-13 | Initial specification, drafted before M0 |

**Open items:** VER-1 to VER-5 (§4.3), sampling rate `R` (§6.3), maturity window
(§11), and all pre-registered thresholds — the last of which must be fixed in
`PREREGISTRATION.md` before M2 begins and must never be revised thereafter.
