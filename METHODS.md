# Bellwether — Methods

Every procedure that produces a published number, in enough detail to recompute
one from the database without asking a question (SRS FR-51).

Written 2026-08-14. Where a figure is provisional, it says so.

---

## 1. The sampling frame

Bellwether does not ingest every English Wikipedia edit. It ingests a
**deterministic probability sample**, and the rule is fixed in code
(`bellwether/frame.py`) rather than chosen per run.

| Stratum | Kept | Why |
|---|---|---|
| Logged out (including temporary accounts) | 50% | The rare-positive class. Reverted at roughly four times the registered rate |
| Registered | 3% | The overwhelming majority of the feed and the overwhelming majority of good edits |

**Deterministic, not random.** Inclusion is `blake2b(salt : revid) mod 100 <
rate`. The same revision is always in or always out, so re-running ingestion
selects the identical set and a falling coverage rate cannot be explained away
as a different draw.

### 1.1 Weight correction

This is **case-control sampling**, so the sample's base rate is not the
population's — it is roughly four times higher. Every rate is therefore
published twice:

- **Raw** — the frequency in the sample.
- **Population-weighted** — each event weighted by `100 / rate`, so a registered
  edit counts 33.3 and a logged-out one counts 2.

Choosing whichever looked better per table would be exactly the quiet selection
this project exists to prevent, so both appear on every table, always.

**Where the weighting matters most:** calibration. A model calibrated against
raw sample frequency would be calibrated to a population that does not exist and
would overstate risk in production by roughly the ratio above.

---

## 2. Labels and maturity

### 2.1 Two label paths

| Path | Source | Nature |
|---|---|---|
| Primary | `mw-reverted` change tag on the reverted edit | Authoritative, applied asynchronously by MediaWiki's `revertedTagUpdate` job |
| Secondary | `mw-undo` / `mw-rollback` / `mw-manual-revert` on the **reverting** edit | Free, derived from tags already stored |

The secondary path is collected for the **whole feed**, outside the sampling
frame, so the ratio between it and the sampled events is a check that the frame
has not blinded the label path.

### 2.2 Why "not yet reverted" is not "not reverted"

An edit that has not been undone has not survived — nobody has looked. Treating
unchecked events as negatives is the single largest source of bias available
here, and it has already happened once in this project: a published rate read
**22.04%** for logged-out editors when the checkpoint data put the same figure at
**38.21%**.

The cause was structural. `landing.rc_events` is insert-only, so its tag array is
frozen at ingestion — for a live-ingested edit it *cannot* contain `mw-reverted`,
because the revert had not happened. Scanning it counted reverts only for rows
backfilled late enough to see them. **The append-only guarantee that makes the
register trustworthy is exactly what made that number wrong.**

### 2.3 The checkpoint grid

Each event is re-checked at **1h, 6h, 24h, 48h and 7 days**. Checking stops once
an edit is labelled positive — a revert found at one hour is still a revert at
six, and re-asking wastes a request against a donated service.

A deterministic **10% maturity cohort** (`blake2b` bucket on revid) receives the
full grid; everything else gets one check, at the seven-day final checkpoint.

### 2.4 Maturity — and the two things that word means

| Sense | Value | What it describes |
|---|---|---|
| M2's window | 48 h (provisional) | When reverts stop arriving. A property of the world, estimated by Kaplan–Meier as the age by which 95% of eventual reverts have landed |
| The metric window | 7 days | A window this pipeline has actually **looked at** |

The metric uses the second, and the difference is not a preference. Outside the
cohort there is exactly one check, at seven days. Using 48 h produced a sample
that was **100% positive** (178 of 178) on the first production run: a positive
qualifies the moment it is found, a non-cohort negative cannot be confirmed until
its seven-day check, and between those points the only gradeable events are the
reverts.

**Inclusion requires both arms:**

1. Elapsed time since the **edit** ≥ window — applied to both classes alike.
2. The outcome actually determined — observed at or beyond the window, **or**
   already known reverted.

Dropping either biases the sample. Requiring the observation arm alone would be
worse than the bias it fixes: the labeller stops checking an edit once it is
positive, so a revert found at one hour never reaches a later checkpoint and
would be excluded permanently — losing precisely the events the model exists to
find.

---

## 3. Features and point-in-time correctness

### 3.1 The fold order

Events are processed in `event_ts` order. Each event is **scored first, then
folded into state**. The same two functions do this in the scorer and in the
replay — one implementation, so there is nothing to drift.

### 3.2 Detection time, not event time

State counters advance when this system **learned** of a revert, not when the
revert happened on Wikipedia. A revert at 12:00 discovered at 14:00 must not
influence a 13:00 prediction, because at 13:00 the scorer could not have known.

This was wrong for part of M3 and the fix changed `state.py` substantially. It is
the reason predictions written before it cannot be re-derived by the code that
replaced it (see §6.2).

### 3.3 The knowability guard

Before any training or scoring run, an automated assertion raises if a feature
depends on a row whose `event_ts` is later than the event it describes. A leaky
feature does not fail on its own — it produces a model that scores beautifully in
backtest and is worthless deployed, and the gap is found weeks later if at all.

---

## 4. Evaluation

### 4.1 PR-AUC as the primary metric

Average precision, chosen because the positive class is rare and ROC-AUC is
optimistic under class imbalance. Pre-registered in `PREREGISTRATION.md` §3
before any model existed.

### 4.2 Two figures that are not the same measurement

| | Backtest | Live |
|---|---|---|
| Data | Backfilled census | The append-only register |
| Labels | Harvested at leisure | Arrived late and unevenly |
| Folds | Trained on data adjacent to what they score | Scored before the outcome existed |
| Value | **0.2560** over 11,188 events | **not yet** — nothing has matured |

Where they disagree, the live one is true. They are published side by side and
labelled, because two accuracy figures shown without that distinction is how a
project ends up quoting whichever is higher.

### 4.3 The paired bootstrap

2,000 resamples, α = 0.05, two-sided, **resampling events rather than models**.
Resampling the models independently would break the pairing and inflate the
standard error, so a genuinely better challenger would fail to clear its margin —
and the failure would look like the challenger's fault rather than the test's.

### 4.4 Calibration

Ten **equal-width** bins, not quantile. The question is whether a score of 0.9
means what it says; quantile edges move every run, so two runs could not be
compared. `score = 1.0` falls in the last bin — half-open bins would drop the
most confident predictions the model makes.

Empty bins are published. A band holding four predictions and one holding four
thousand look identical without their counts.

**ECE** is the count-weighted mean absolute gap between mean predicted and
population-weighted observed rate.

### 4.5 PSI (drift)

Bin edges are quantiles of the **training** distribution, frozen at training
time. Whether today resembles yesterday is not the question; whether today
resembles what the model was fitted to is.

Laplace-smoothed, so one unseen value cannot send a term to infinity and fire a
retrain alone. Declines to answer below 50 events per side, or when the reference
is constant — a constant cannot drift, and reporting 0.0 would be
indistinguishable from having checked.

The score distribution is monitored against the champion's **own first week of
live scoring**, not its scores on the training data. Those are in-sample, and
comparing them to live scores would measure the generalisation gap and call it
drift — in-sample 0.729 against out-of-sample 0.256 is the size of that error.

### 4.6 Cohen's κ (label quality)

`κ = (p_o − p_e) / (1 − p_e)`, human verdict against the revert proxy, with
"bad" and "reverted" as the positive classes.

- **Undefined, not zero**, when expected agreement is 1.0. Zero reads as "no
  better than chance"; the truth is there is no chance model to compare against.
- **Not published below n = 100** matured labels.
- **Below two reviewers**, labelled as agreement between the proxy and one
  person rather than a property of the proxy.
- Computed on the **random slice** only. A figure over the ranked slice measures
  agreement among edits the model already flagged.

---

## 5. Every exclusion, and the direction of its bias

A methods document that lists exclusions without their direction has explained
nothing. All of these are defensible. **None of them are neutral.**

| Excluded | Count | Direction of bias |
|---|---|---|
| **Immature predictions** | most of the register | Skews toward recent events. Removes nothing systematically *if* both arms of §2.4 are applied; removes almost all negatives if only one is |
| **Late-scored predictions** — written after their own outcome was already visible | 2,149 of 48,064 (4.5%) | **Concentrates in fast reverts.** Dropping them removes real positives, so the remaining sample is slightly *harder* than the true population. Their own base rate is published beside the count |
| **`unsure` human verdicts** | reported per run | **Concentrates in ambiguous cases** — exactly where the proxy is most likely to disagree with a human. Dropping them selects on the outcome being studied, so κ is computed both ways |
| **The training window, in scoring** | whole window | Prevents the register measuring in-sample behaviour. Removes nothing from the live figure that belonged in it |
| **Events outside the frame** | 97% of registered edits | Corrected by weighting, not ignored (§1.1) |
| **Predictions from a superseded model, in reproducibility** | 180 of 1,694 | Narrows the claim to the serving model. See §6.2 |

---

## 6. Reproducibility

### 6.1 What is checked

A deterministic 5% sample of the register is re-derived daily from the raw
events and must produce the same feature hash and the same score. The replay is
the **scored set in scoring order** — `register.predictions` joined to
`rc_events`, ordered by `(scored_at, event_ts, revid)` — not a time window over
events. Three attempts were needed to establish that; a 2-day window gave 98.99%
and a 30-day window gave 61.90%, which disproved the obvious hypothesis rather
than confirming it.

Only the hash is stored, never the vector. So a mismatch says something differs
without saying what — which is a real limitation and is stated rather than
papered over.

### 6.2 What the claim covers, and what it does not

**The claim is scoped to the serving model.** Predictions written by a superseded
champion are reported separately and are not counted as failures.

The reason is §3.2. `state.py` changed substantially when detection-time folding
was fixed, and a prediction written before that fix **cannot** re-derive under
the code that replaced it. Production reported 180 of 1,694 unreproducible and
the daily job went red, when what had actually happened was a correctness fix
landing.

Counting that as a reproducibility failure would have been false. Counting it
silently as a success would have been worse. It is reported in its own column,
and the guarantee is stated at the width it can actually be supported:

> We can reproduce what the serving model produced. We cannot reproduce
> predictions written by code that has since been corrected, and we say which
> ones those are.

---

## 7. The decision procedures

Fixed in `PREREGISTRATION.md` before the first model was trained, held as
constants in `bellwether/preregistration.py`, and asserted against that document
by the test suite — so the two cannot drift apart.

**Retrain triggers** (any one): rolling 7-day PR-AUC more than 0.03 below the
champion's registered baseline; PSI > 0.20 on a monitored feature or the score
distribution; or 7 days since the last training run. The first two require **3
consecutive daily windows** — one bad day on a rare-positive metric is noise, and
a system that retrains on noise is twitching rather than maintaining itself. A
gap resets the count.

**Promotion** (all five): PR-AUC gain ≥ 0.02 on the same matured events; the
paired bootstrap 95% interval excludes zero; ≥ 2,500 matured positives in
shadow; ≥ 7 days of wall-clock shadow; ECE no worse by more than 0.02 and no
pre-registered segment regressing by more than 0.03.

**Rollback**: rolling 7-day PR-AUC more than 0.02 below the previous champion's
registered level within 14 days of promotion, measured from the **decision**
rather than the training date.

The rolling window is over the last seven days of **evidence**, not of events.
Maturity is seven days, so a window over recent events is empty by construction —
the first implementation was, and it would have silently disabled the decay
trigger and the rollback check together.

---

## 8. What a reader can recompute

Everything above is derivable from the public endpoints:

| | |
|---|---|
| `/stats` | Ingestion, coverage gaps, matured revert rates raw and weighted |
| `/register` | Prediction count, scoring lag distribution, late-score count, reproducibility |
| `/metrics`, `/metrics/history` | Live figures per population and window, every one with its `n` |
| `/calibration` | Reliability bins, raw and weighted |
| `/kc2` | The backtest, the margin, and the ablation |
| `/decisions` | Every promotion, rejection and rollback with its evidence |
| `/agreement` | κ per slice and `unsure` treatment, or the reason it is refused |

Model artifacts and their metric cards are committed to the repository, so a
decision is verifiable from git alone by someone with no database access.
