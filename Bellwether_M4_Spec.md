# Bellwether — M4 Spec

**Continuous evaluation.**
Rolling metrics on matured predictions, segments, calibration, and the Lift Wing
benchmark.

---

## 1. What M4 is for

M3 committed 10,000 forecasts to a table nobody can edit. Not one of them has
been graded.

M4 is where the project finds out whether its predictions are any good — on
predictions it actually made, in production, before the answers existed. Every
number published so far comes from an offline backtest over a backfilled census
(`/kc2`, PR-AUC 0.256). That is a rehearsal. This is the performance.

The distinction matters more than it sounds. The backtest scored a window the
model was fitted near, on events ingested in one regime, with labels harvested
at leisure. The register holds live predictions on a different population,
written under a scoring lag, graded by outcomes that arrive late and unevenly.
If those two numbers disagree, the second one is the true one.

### 1.1 Recorded before measuring

**The live number will be worse than 0.256, and it should be.** Written here so
it cannot be quietly reframed afterwards:

- the backtest window is the 08-10 backfilled census; the register is live data
  from 08-13 onward, a different ingestion regime with different label
  completeness
- the backtest's rolling-origin folds train on data adjacent to what they score;
  the register's champion was trained on 08-10 and is scoring days later with no
  refit
- the first matured cohorts will be small enough that the confidence interval
  will be wide enough to contain almost anything

If the live PR-AUC comes back *higher* than 0.256, that is a finding to be
suspicious of rather than pleased about, and M4-FR-19 exists to make the first
suspicion cheap to check.

---

## 2. The risks M3 handed over

| # | Inherited | Consequence for M4 |
|---|---|---|
| I-1 | The KC-2 margin rests on one feature; ablating `account_newness` drops it to +0.0386, below the bar | Segment metrics must report per-feature-regime performance, not just an aggregate that hides it |
| I-2 | 12 of 28 features measure at exactly 0.000 importance | A feature set half of which does nothing is a decision M4 must surface, not silently carry into M5's retraining |
| I-3 | The maturity window is a 48h placeholder; the cohort ages ~21 August | Every M4 metric is provisional until then, and must say so in the same payload as the number |
| I-4 | Scoring lag p50 is 580 minutes while the backlog drains | Lag is a confounder: late-scored predictions are excluded (M3-FR-10), and exclusion is not neutral if it correlates with the outcome |
| I-5 | `reproduce` replays every scored prediction in its window | Will exceed its timeout at ~280,000 predictions; M4 adds load to the same tables |

I-4 is the subtle one. Excluding predictions whose outcome was already
observable is correct, but those exclusions are not a random sample — they are
concentrated in edits that were reverted fast. Dropping them removes real
positives, and a metric computed on what remains is measuring a population the
system did not actually face.

| # | Requirement |
|---|---|
| M4-FR-1 | Every published metric shall carry `n`, a confidence interval, and the maturity window it used |
| M4-FR-2 | Exclusions shall be published as counts alongside every metric, never applied silently |
| M4-FR-3 | The share of matured predictions excluded for late scoring shall be reported **with its own base rate**, so a reader can see whether exclusion is selecting on the outcome |

---

## 3. Rolling metrics on the register

The primary evaluation. Matured predictions only, graded against
`outcome.labels`, computed on a schedule and stored as evidence.

| # | Requirement |
|---|---|
| M4-FR-4 | A scheduled job shall compute metrics over rolling windows (7d, 30d, all-time) on matured predictions in `register.predictions` |
| M4-FR-5 | Maturity shall be enforced **in the metric query itself** (SRS FR-22, R-3), never by a filter the caller may forget |
| M4-FR-6 | A prediction whose outcome has not matured shall be excluded, never counted as a negative |
| M4-FR-7 | Predictions flagged `outcome_observable_at_scoring` shall be excluded from every accuracy figure (M3-FR-10) |
| M4-FR-8 | Every rate shall be published raw **and** population-weighted, per M1-FR-3 |
| M4-FR-9 | Results shall be written to an append-only `outcome.prediction_metrics`, so a run that produced a bad number cannot be re-run away |

### 3.1 The primary metric is fixed, and was fixed before M0

`PREREGISTRATION.md` names **PR-AUC** as primary. M4 does not get to prefer
ROC-AUC because the class balance makes it look better, and does not get to
promote a segment to headline because it beat the aggregate.

| # | Requirement |
|---|---|
| M4-FR-10 | PR-AUC shall be the headline metric. ROC-AUC, Brier and lift@k may be published beside it, never instead of it |
| M4-FR-11 | The baseline comparison shall remain the logged-out heuristic, on the same events, paired |

---

## 4. Segments

Segments are where evaluation projects quietly become fishing expeditions. Ten
segments and one will look significant.

| # | Requirement |
|---|---|
| M4-FR-12 | The segment list shall be **fixed in this spec** before any segmented number is computed, and changing it requires an amendment recorded in git |
| M4-FR-13 | Every segment shall be published every run, including the ones that look bad |
| M4-FR-14 | No segment shall be reported as a headline result. The aggregate is the result; segments are diagnosis |

The list, fixed here:

| Segment | Levels | Why |
|---|---|---|
| `sampling_stratum` | logged_out, registered | The frame samples these at 50% and 3%; performance almost certainly differs and the weighted aggregate hides it |
| `editor_has_history` | yes, no | I-1: the margin rests on `account_newness`, which is uninformative for editors we have never seen |
| `namespace` | article (0), other | Vandalism patterns differ sharply off mainspace |
| `scoring_lag_bucket` | ≤30 min, >30 min | I-4: if performance differs by lag, the backlog is confounding every other number |

Four segments, chosen for what they might falsify rather than for coverage.

---

## 5. Calibration

A ranking model can be perfectly ordered and still claim 0.9 for things that
happen a tenth of the time. M6 puts a queue in front of a human; a score that
does not mean what it says is worse than no score.

| # | Requirement |
|---|---|
| M4-FR-15 | A reliability curve shall be computed over deciles of predicted score, with observed frequency and `n` per bin |
| M4-FR-16 | Brier score shall be decomposed into reliability, resolution and uncertainty |
| M4-FR-17 | Calibration shall be computed on **population-weighted** outcomes, not raw sample frequency |

M4-FR-17 is the one that is easy to get wrong. The frame keeps 50% of logged-out
edits and 3% of registered ones, so the sample's base rate is roughly four times
the population's. A model calibrated against raw sample frequency would be
calibrated to a population that does not exist, and would systematically
overstate risk in production.

---

## 6. The Lift Wing benchmark

Wikimedia runs `revertrisk-language-agnostic` in production. The SRS §6.4
already recorded, before any model existed, that it is expected to win.

| # | Requirement |
|---|---|
| M4-FR-18 | A sample of matured predictions shall be scored by Lift Wing and compared **paired, on the same events, under the same maturity window** |
| M4-FR-19 | The comparison shall be published whichever way it falls, with the paired difference and its CI |
| M4-FR-20 | Lift Wing scores shall be stored with the revision id and fetch timestamp, so the comparison is reproducible after the service changes |
| M4-FR-21 | If Lift Wing is unavailable or gated, that shall be recorded as an unmet dependency — not worked around by substituting a different benchmark |

### 6.1 What this benchmark is for

Not to win. The deliverable was never "beat Wikimedia" — it is a system that
grades itself honestly and maintains itself unattended. A benchmark that only
gets published when it flatters is not a benchmark, and the pre-registration of
the expected loss is what makes the eventual number worth reading.

**Risk.** DS-3's auth column reads `TBC` in the SRS. Lift Wing has moved toward
requiring an access token for some endpoints. M4-FR-21 exists so that a gated
API produces an honest gap in the deliverable rather than a quietly swapped
comparator.

---

## 7. Publication

| # | Requirement |
|---|---|
| M4-FR-22 | `/metrics` shall serve the latest rolling metrics, all segments, exclusion counts and the maturity window used |
| M4-FR-23 | `/calibration` shall serve the reliability curve and the Brier decomposition |
| M4-FR-24 | The status page shall show the live PR-AUC beside the backtest figure, labelled as different things |

M4-FR-24 matters. Two numbers that measure different populations displayed
without that distinction is how a project ends up quoting whichever is higher.

---

## 8. Storage

At ~9,300 predictions a day, metrics rows are negligible; Lift Wing scores are
not free.

| | |
|---|---|
| `prediction_metrics` | one row per (window, segment, run) — a few hundred rows a day at most |
| `liftwing_scores` | ~40 B a row, sampled rather than exhaustive |

| # | Requirement |
|---|---|
| M4-FR-25 | Lift Wing shall be sampled, not exhaustive, with the sample rate published beside every comparison |
| M4-FR-26 | Metrics and Lift Wing scores shall join the monthly seal manifest, pruned on the M1 mechanism unchanged |

---

## 9. Acceptance criteria

| # | |
|---|---|
| D-1 | Live PR-AUC on matured register predictions is published, with `n` and CI |
| D-2 | The metric query enforces maturity itself — demonstrated by a test that removes the filter and sees the number change |
| D-3 | Immature predictions are excluded, never counted as negatives — demonstrated |
| D-4 | Late-scored predictions are excluded, and the exclusion's own base rate is published |
| D-5 | Every rate appears raw and population-weighted |
| D-6 | All four segments published every run, including unflattering ones |
| D-7 | Reliability curve and Brier decomposition published, population-weighted |
| D-8 | Lift Wing comparison published paired with its CI — or recorded as an unmet dependency |
| D-9 | Metrics are append-only; a re-run cannot overwrite a previous result |
| D-10 | The status page distinguishes the live figure from the backtest figure |

---

## 10. What M4 must not become

**A dashboard.** The temptation is to build fifteen charts and call the
milestone delivered. The deliverable is a small number of pre-registered
metrics, computed the same way every time, published whether or not they are
good.

**A place where the metric gets chosen.** PR-AUC is fixed. Segments are fixed in
§4. If a different metric would be better, that is an amendment with a date on
it, made before the number is seen.

**An excuse to retrain.** M4 measures. It does not touch the champion. The
moment evaluation starts feeding back into model selection outside M5's
pre-registered rule, every number the project has published becomes an in-sample
number.

**A milestone that reports only aggregates.** I-1 says the margin rests on one
feature. An aggregate PR-AUC that stays healthy while the segment for editors
with no history collapses is not a system working — it is a system whose failure
is being averaged away.
