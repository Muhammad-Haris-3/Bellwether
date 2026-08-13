# Bellwether — Pre-registration

**Committed:** 2026-08-13, during M0
**Status:** Fixed. Not to be revised.

---

## Why this document exists

Bellwether's claim is that it replaces its own model when the evidence says it
should, under a rule nobody chose after seeing the result. That claim is worth
exactly as much as the timestamp on this file.

Every threshold below is fixed **before any model exists**. `model_registry` is
empty as this is committed, and acceptance criterion AC-3 asks a reader to
verify that from git history alone.

If a threshold later proves badly chosen, the finding is recorded in the
milestone summary and **this document is left standing**. A record of a
commitment that turned out wrong is worth more than a document quietly edited
to match the outcome.

---

## 1. What is being measured

| | |
|---|---|
| **Task** | For each English Wikipedia main-namespace edit, the probability it will be reverted |
| **Population** | The sampling frame in SRS §6.3: all logged-out edits, plus a deterministic hash sample of registered edits |
| **Eligible for metrics** | Matured predictions only — an edit whose outcome has had time to arrive (§6) |
| **Excluded** | Bot edits, non-main namespaces, revisions deleted before labelling |

## 2. Primary metric

**PR-AUC** (average precision) over matured predictions.

Chosen because the positive class is rare — 5.6% of edits overall, and 3.3%
among registered editors. ROC-AUC is dominated by the majority class at these
rates and rewards a model for correctly ignoring edits nobody would review.

ROC-AUC is reported as a secondary figure, never as the decision criterion.

## 3. Secondary metrics

Reported always; two of them can veto a promotion (§5).

| Metric | Why |
|---|---|
| **Precision@k** at the operational queue depth | What a reviewer actually experiences |
| **Recall@k** at the same depth | What the wiki actually gets protected from |
| **Brier score** | Overall probabilistic accuracy |
| **Expected calibration error (ECE)**, 10 equal-count bins | Whether a stated 0.3 means 30% |
| **ROC-AUC** | Comparability with published work |

Metrics are reported both raw and corrected to the population prior, since the
frame is case-control (SRS §6.3). Both are always shown; neither is dropped in
favour of whichever looks better.

## 4. Segments

Every metric is broken down by:

- **editor class** — logged out (including temporary accounts) vs registered
- **edit size band** — bytes added/removed, quartiles of the training window
- **hour of day**, UTC
- **page activity band** — edits to that page in the prior 7 days, quartiles

## 5. The promotion rule

A challenger replaces the champion **only if all five hold**:

| # | Condition |
|---|---|
| P-1 | PR-AUC exceeds the champion's by **at least 0.02 absolute**, on the same matured events |
| P-2 | The paired bootstrap 95% interval for that difference **excludes zero** |
| P-3 | At least **2,500 matured positives** have accumulated in shadow (§7) |
| P-4 | At least **7 days** of wall-clock shadow running have elapsed |
| P-5 | ECE is no worse than the champion's by more than **0.02**, and **no segment** in §4 regresses in PR-AUC by more than **0.03** |

Failing any condition, the challenger is **rejected** and the champion stays.
Rejection is recorded in `decide.model_decisions` with the same evidence a
promotion would carry.

P-4 exists independently of P-3 because sample size and calendar time are not
interchangeable: 2,500 positives drawn from a single quiet weekend is not
evidence a model works on a Monday.

### The test

Paired bootstrap over **events**, 2,000 resamples, α = 0.05, two-sided.

Resampling events keeps each model's two scores together. Resampling the models
independently would break the pairing and inflate the standard error, so a
genuinely better challenger would never be promoted — and the failure would
look like the challenger's fault rather than the test's.

## 6. Maturity

A prediction may not enter any published metric until its outcome is final.

- The maturity window is estimated in **M2** by Kaplan–Meier over the
  checkpoint grid, as the age by which **95%** of eventual reverts have arrived.
- The **method is fixed now**; the number is measured later. Fixing a number
  before measuring the curve would be a guess wearing a commitment's clothes.
- **If the survival curves differ materially by editor class, separate windows
  are used per stratum.** M0 saw ~44% of logged-out reverts land within an hour
  against ~21% for registered ones (different samples, so a hypothesis, not a
  finding). A single global window would systematically undercount registered
  reverts — bias landing precisely on the split the model ranks by.
- Backtest and live results are stored and displayed in **separate columns and
  never pooled**.

## 7. Minimum sample: how 2,500 was derived

Computed by simulation in [`scripts/power_calculation.py`](scripts/power_calculation.py),
seeded and committed so any reader can reproduce it:

```bash
python scripts/power_calculation.py
```

**Inputs** — measured on 20,000 matured edits, 2026-08-13:

| | |
|---|---|
| Revert rate, logged out | 22.25% |
| Revert rate, registered | 3.26% |
| Logged-out share of edits | 15.7% |
| Positive rate in the sampled evaluation set | 12.4% |

**Assumptions**, stated because the answer depends on them:

| | | |
|---|---|---|
| Champion PR-AUC | 0.35 | Plausible for this task; the required N is insensitive to it near this value |
| Between-model correlation ρ | 0.85 | Two models on the same features, trained a week apart |

**Result:**

| N (events) | Positives | SD of PR-AUC difference | Power |
|---|---|---|---|
| 5,000 | 620 | 0.0115 | 0.41 |
| 10,000 | 1,241 | 0.0079 | 0.72 |
| **20,000** | **2,483** | **0.0053** | **0.96** |

Verified two ways: the paired bootstrap's standard error matched the true
sampling spread to within 0.5%, and re-running the actual decision rule 60
times at N = 20,000 gave power **0.95**.

The 80% point lies between 10,000 and 20,000 events. **The conservative grid
point is pre-registered.**

Expressed in **positives**, not total events, deliberately — the registered
sampling rate `R` is set in M1, and a requirement stated in total events could
be moved afterwards by changing `R`. A requirement in positives cannot.

## 8. What triggers a retrain

Any one of:

| Trigger | Condition |
|---|---|
| **Decay** | Rolling 7-day PR-AUC falls below the champion's registered baseline by more than **0.03**, on **3 consecutive daily windows** |
| **Input drift** | **PSI > 0.20** on any monitored feature or on the score distribution, on **3 consecutive daily windows** |
| **Floor** | **7 days** since the last training run, regardless of either above |

Three consecutive windows, not one: a single bad day on a rare-positive metric
is noise, and a system that retrains on noise is not maintaining itself, it is
twitching.

## 9. Rollback

If a newly promoted champion's rolling 7-day PR-AUC falls below **the previous
champion's registered level** by more than **0.02** within **14 days** of
promotion, the previous champion is **automatically restored**.

Rollback is a `model_decisions` row like any other. It is not an error state
and is not hidden: a system that can promote but never retreat has only half a
mechanism.

## 10. Benchmarks

The following are computed on identical matured events and published whatever
they show:

- **Arrival order** — no model at all
- **Logged-out heuristic** — rank every logged-out edit above every registered one
- **Size-delta heuristic** — rank by absolute bytes changed
- **Wikimedia Lift Wing `revertrisk-language-agnostic`**

**Bellwether is expected to lose to Lift Wing.** A 16-revision probe on
2026-08-13 separated the classes completely (SRS §6.4). That expectation is
recorded here so the comparison cannot later be dropped, reframed, or replaced
with a weaker opponent. If Lift Wing wins, the result is published in the
decision memo with the same prominence a win would receive.

**KC-2 is unchanged and is the only kill criterion on model quality:** if no
leak-free feature set beats the logged-out heuristic by a meaningful margin,
the project stops at M2.

## 11. What is deliberately not pre-registered

- Feature set, model family, hyperparameters. These are free to change; the
  rule that judges them is not.
- The sampling rate `R` (M1) and the maturity window (M2), both of which are
  measured. Their **methods** are fixed above.

## 12. Amendments

None permitted. Deviations discovered later are recorded in the relevant
`Bellwether_M<n>_Summary.md`, alongside what was originally committed here and
why the original was wrong. The public model timeline (FR-44) links to this
file at the commit it was registered.
