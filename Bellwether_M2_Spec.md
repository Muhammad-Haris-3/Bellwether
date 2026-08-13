# Bellwether — M2 Specification

**Milestone:** M2 — Maturity, features, and the kill criterion
**Date:** 2026-08-14
**Status:** Not started
**Depends on:** `Bellwether_SRS_v1.0.md`, `PREREGISTRATION.md`, `Bellwether_M1_Summary.md`

---

## 1. The two questions M2 answers

> **When is an outcome final?** Every accuracy figure this project publishes is
> computed over matured predictions. The window that decides which predictions
> qualify is not yet known.

> **Is there any signal here at all?** KC-2 says that if no leak-free feature
> set beats the trivial logged-out heuristic by a meaningful margin, the project
> stops at M2.

M2 is the milestone that can end the project, and it should be built in a way
that makes ending it easy to see.

---

## 2. The maturity window

### 2.1 What the data already shows

Cumulative incidence of reverts by edit age, from `/maturity`, 2026-08-14:

| Age | Logged out | Registered |
|---|---|---|
| 1 h | 10.62% | 1.03% |
| 6 h | 19.22% | 2.10% |
| 24 h | 22.12% | 2.51% |
| 48 h | **38.21%** | **5.73%** |

**The curves have not flattened.** The 7-day checkpoint has no observations at
all — the oldest data is about four days old — so the age at which reverts stop
arriving is still unmeasured. Setting a window from this table would be
guessing with extra steps.

### 2.2 The M0 hypothesis, now measured on the same events

| | Share of eventual reverts landing in the first hour |
|---|---|
| Logged out | ~28% |
| Registered | ~18% |

M0 raised this across two samples from different days and could not separate it
from day-of-week effects. It now holds within a single population observed at
successive ages: **registered-user reverts arrive more slowly**.

`PREREGISTRATION.md` §6 already permits separate windows per stratum if the
curves differ. They differ. Whether they differ *enough* to justify two windows
is M2's to decide, against a criterion fixed before the estimate is complete
(§2.3).

### 2.3 What M2 must do

| # | Requirement |
|---|---|
| M2-FR-1 | Estimate the revert-latency survival curve by Kaplan–Meier from the maturity cohort, per stratum, honouring right-censoring |
| M2-FR-2 | Set the maturity window at the age by which **95%** of eventual reverts have arrived, as pre-registered |
| M2-FR-3 | Use **separate windows per stratum** if the 95% ages differ by more than 25%, and a single pooled window otherwise. This threshold is fixed here, before the curve is complete |
| M2-FR-4 | Extend the checkpoint grid beyond 7 days if the curve has not flattened by then, and say so rather than truncating at the last checkpoint that happens to exist |
| M2-FR-5 | Publish the curve, its confidence bands and the chosen window, and re-estimate on a schedule — a maturity window fixed forever is a claim that editor behaviour never changes |

---

## 3. Features, and two ways to leak the answer

### 3.1 The trap that is already in the database

Backfilled rows carry `mw-reverted` **in their ingestion-time tag array**,
because they were fetched days after the edits were reverted. Live rows never
can, for the same structural reason M1's matured-rate bug had: `rc_events` is
insert-only and the array is frozen at ingestion.

So a model trained on `tags` would find a **perfect predictor on backfilled rows
and nothing at all on live ones**. It would score superbly in any backtest that
included the backfill, and be worthless the moment it was deployed.

This is the single most dangerous thing in the project's current data.

| # | Requirement |
|---|---|
| M2-FR-6 | `mw-reverted` shall never enter a feature, directly or through any aggregate over `tags` |
| M2-FR-7 | The knowability guard shall **raise** on any feature whose value differs between a backfilled row and an equivalent live row for reasons of ingestion timing |

### 3.2 The trap that looks like a good idea

"How many edits has this user made" is the obvious feature and is fatal if read
from the API at training time: the API returns *today's* count, not the count at
the moment of the edit. SRS §3.4 names this as Threat 1.

Every editor- and page-derived feature must therefore come from Bellwether's own
accumulated history, in event order.

| # | Requirement |
|---|---|
| M2-FR-8 | `editor_state` and `page_state` shall be materialised strictly in event order from ingested events |
| M2-FR-9 | Every feature shall be computable from events with `event_ts` strictly less than the subject event's |
| M2-FR-10 | The knowability guard shall assert FR-9 for every feature build and shall **raise**, failing the job — never warn |
| M2-FR-11 | Feature vectors shall be persisted with a `feature_hash`, so any historical prediction can be reproduced exactly |

### 3.3 Editor history is itself a sample — and this may not work

The frame keeps 3% of registered edits. So "edits by this user we have seen" is
roughly 3% of their real activity, and most registered editors will appear with
a prior history of **zero**.

This is point-in-time correct and consistently biased, which makes it *usable*
but possibly *useless*. M2 must measure whether it carries signal rather than
assume either way.

Two things soften it, and both should be exploited before concluding the feature
is dead:

- **`revert_events` is outside the frame.** Reverting activity is observed for
  the whole feed, so "times this user's edits have been reverted, as we saw it"
  is far better populated than their edit count.
- **`user_id` is monotonically increasing** with account creation and is present
  on the event itself. It is an account-age proxy that needs no history at all
  and cannot leak, because it was fixed before the edit existed.

| # | Requirement |
|---|---|
| M2-FR-12 | The coverage of `editor_state` shall be measured and published — what fraction of scored events have any prior history at all, per stratum |
| M2-FR-13 | If that coverage is below 10% for a stratum, history-derived features shall be reported as uninformative for it rather than included and left to look like signal |

> **Measured 2026-08-14, and the concern above was wrong.** Coverage on framed
> rows only — census rows excluded, because they were ingested at 100% and
> would flatter the figure by about thirty points:
>
> | Stratum | Events with prior editor history |
> |---|---|
> | Logged out | **42.2%** |
> | Registered | **50.0%** |
>
> Comfortably above the 10% threshold in both strata, so history features stay
> in. The reasoning that predicted otherwise treated editors as
> interchangeable; edit activity is in fact heavily concentrated, so even a 3%
> sample catches prolific editors repeatedly — and prolific editors are most of
> the events.
>
> This is a lower bound: it was measured over a few hours of framed data, and
> coverage can only rise as history accumulates. The requirement stands anyway,
> because the frame may change and the day it does this number should be
> re-checked rather than remembered.

### 3.4 The candidate feature set

Available from the event alone, with no extra request and no history:

| Group | Features |
|---|---|
| Editor class | logged out, temporary account, `user_id` magnitude |
| Size | signed and absolute byte delta, new page, blanking |
| Summary | present, length, section edit, contains a link |
| Tooling | tag ids **excluding** any revert-outcome tag |
| Time | hour of day, day of week, both cyclically encoded |

From accumulated history, point-in-time:

| Group | Features |
|---|---|
| Editor | edits seen, reverts seen, time since first seen |
| Page | edits seen, reverts seen, distinct editors seen |

---

## 4. Baselines, and the criterion that can end the project

Three baselines, computed on identical matured events:

| Baseline | Definition |
|---|---|
| Arrival order | No model. The status quo for a patroller |
| **Logged-out heuristic** | Rank every logged-out edit above every registered one |
| Size-delta heuristic | Rank by absolute bytes changed |

The middle one is the opponent that matters. M1 measured it at **22% against
3.3%** — a 6.8× lift from a single boolean already present on every event.

**KC-2 stands:** if no leak-free feature set beats it by a meaningful margin,
the project stops at M2.

| # | Requirement |
|---|---|
| M2-FR-14 | "Meaningful margin" is **+0.05 PR-AUC absolute** over the logged-out heuristic, on matured events, with a paired bootstrap interval excluding zero. Fixed here, before any model exists |
| M2-FR-15 | Model training shall use **rolling-origin** evaluation, never a random split, because the data is temporally ordered |
| M2-FR-16 | Each trained model shall be registered with version, artifact hash, training window, hyperparameters, feature list and offline metrics |
| M2-FR-17 | Lift Wing's revert-risk score shall be computed on the same matured events and reported alongside, expected to win (SRS §6.4) |

A margin fixed after seeing the result is not a criterion. +0.05 is chosen to be
larger than the noise a few thousand positives can produce, and small enough
that a genuinely useful model clears it.

---

## 5. Acceptance criteria

| # | Criterion |
|---|---|
| C-1 | A Kaplan–Meier curve per stratum, with confidence bands, published and reproducible from `/maturity` |
| C-2 | The maturity window set by the pre-registered 95% rule, and the one-window-or-two decision made by the §2.3 threshold |
| C-3 | The knowability guard **raises** on a deliberately planted leaky feature — demonstrated, not asserted |
| C-4 | `mw-reverted` provably absent from every feature, verified by a test that would fail if it returned |
| C-5 | `editor_state` coverage measured and published per stratum |
| C-6 | A baseline model trained with rolling-origin evaluation, registered with its artifact hash |
| C-7 | PR-AUC of the model and all three baselines on identical matured events, with paired intervals |
| C-8 | **KC-2 answered in public** — either the margin is cleared, or the project stops and says so |

---

## 6. What M2 must not become

- **Model tuning.** The question is whether signal exists, not how much can be
  extracted from it. A week of hyperparameter search to clear +0.05 would be
  answering a different question than KC-2 asks.
- **Feature engineering ahead of the guard.** The guard is built first. Features
  written before it exists will be checked by someone who already wants them to
  work.
- **Quietly dropping the backfilled rows.** They are 40% of the data and their
  tag arrays are contaminated. Excluding them from training is legitimate;
  excluding them without saying so, and reporting metrics as if the sample were
  the same, is not.
- **Deciding KC-2 after seeing the number.** The margin is fixed in §4. If the
  result lands at +0.04, the project stops.

---

## 7. Order of work

1. Knowability guard, with a planted leak to prove it fires (C-3, C-4)
2. `editor_state` / `page_state`, and the coverage measurement (C-5)
3. Kaplan–Meier and the maturity window (C-1, C-2) — needs the 7-day checkpoint
4. Baselines on matured events (C-7)
5. The model, once and without tuning (C-6)
6. KC-2, published either way (C-8)

The guard comes first on purpose. Everything after it is only trustworthy
because it exists.
