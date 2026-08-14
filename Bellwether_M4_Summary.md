# Bellwether — M4 Summary

**Continuous evaluation.**
Written 2026-08-14. 262 tests. 26 of 26 requirements built; **8 of 10
acceptance criteria satisfied, 2 pending data**.

This summary is deliberately written before the milestone can be closed. Every
mechanism M4 promised exists, is scheduled, and has been exercised in
production — and the headline number it was built to produce does not exist
yet, because nothing in the register has matured. Publishing a summary that
said "continuous evaluation works" while every figure read *not yet* would be
the same vacuous pass this project keeps catching in its own machinery.

---

## 1. Status

| | |
|---|---|
| Requirements built | 26 of 26 |
| Acceptance criteria met | 8 of 10 |
| **Live PR-AUC** | **not yet — no scored edit has matured** |
| **Lift Wing comparison** | **not yet — 55 paired scores, needs matured labels** |
| Cohort figure expected | ~2026-08-16 |
| Full-population figure expected | ~2026-08-20 |

Nothing is blocked. Both pending criteria are waiting on the calendar, and the
dates are consequences of decisions recorded in §2.

---

## 2. Findings

Four wrong hypotheses of mine were killed by data during this milestone, and
one green workflow turned out to mean nothing. That ratio is the milestone.

### 2.1 The first live metric was 100% positives

The first production run graded 178 predictions and every one was a positive.
Base rate 1.000, PR-AUC undefined.

Not a coding error. The maturity rule said an event was matured when it had
been observed for the window **or** was already known reverted — so a revert
qualified the moment it was found while a negative had to wait the window out.
At any instant the gradeable set was therefore the reverts, and the metric
would have reported a perfect base rate indefinitely with nothing appearing
broken.

**The obvious repair is worse.** `label.py` stops checking an edit once it is
labelled positive, so a revert found at one hour has `last_observed_age` frozen
at one hour permanently. Requiring an observation at or beyond the window would
have excluded every early revert *for good* — losing precisely the events the
model exists to find, silently.

Inclusion is now elapsed time since the **edit**, applied to both classes
alike, **and** the outcome being determined by either arm. Both conditions;
dropping either biases the sample one way or the other. Both failure modes are
regression tests.

### 2.2 Two different things were being called maturity

M2's 48 hours describes when reverts stop arriving — a property of the world,
estimated from the survival curve. The window a metric needs is one this
pipeline has actually **looked at**, and outside the 10% maturity cohort the
labeller checks exactly once, at the seven-day final checkpoint.

Grading needs both, and the binding constraint is observation rather than the
world. At seven days both arms become available at the same moment, which is
what makes the sample unbiased rather than merely larger.

The cost is stated rather than absorbed: **nothing is gradeable until the
register is seven days old.**

### 2.3 The cohort covers less than it claimed

The maturity cohort is 0.93% of the table, not the tenth M4 assumed, and the
spec's claim that it is "a probability sample of the frame" was wrong as
written.

The flag is set at insert time. Roughly 49,000 rows were inserted before that
code shipped, and `ON CONFLICT DO NOTHING` means re-ingesting never corrects
them. Recent ingest runs flag 8.5–12.7%, so nothing is broken now — the cohort
simply begins partway through, and is a probability sample of events **from
that point on**.

Three of my hypotheses died here before the per-run figures settled it: that
the bucket was wrong (it selects 9.76% of realistic revision ids), that the
split was by ingestion date (every row was ingested on one day), and that the
scorer had not reached the flagged events (the first cohort event predates the
newest scored one).

**Not backfilled**, though the bucket is a pure function of `revid`. The
labeller used the *stored* flag to decide which events received the dense
checkpoint grid, so a backfilled row would be marked a cohort member while
holding none of the 48-hour checks the cohort exists to provide — a sample
claiming an observation nobody made.

### 2.4 Lift Wing is open, and one 503 was reported as it being down

The benchmark endpoint answers without a token: 55 scores returned on the first
real run. **This closes DS-3's `TBC` in the SRS** — no credentials are needed,
and M4-FR-21's gated path stays unexercised rather than becoming the outcome.

Then a single 503 ended that run at 55 of 200 and it was recorded as *the
service being unavailable*. It was a blip on someone else's server. Every other
upstream call in this project retries 5xx with backoff and honours
`Retry-After`; this client was written without any of it, so the least reliable
moment decided the whole batch. Now retried, with a circuit breaker at five
consecutive failures so "one revision failed" and "the service is down" stay
distinguishable.

### 2.5 Two of my own bugs, of a shape this project keeps finding

**A column queried and never served.** `cohort_events` was added to
`STATS_SQL` and not to the response, which builds its keys by hand. Nothing
failed; the field was simply absent, indistinguishable from a column nobody
asked for. The same vacuous shape as migration 005 having no health
expectation. A test now reads the aliases out of the query and requires each to
appear.

**A mangled line continuation, shipped twice.** Editing workflow YAML through a
shell heredoc turned `\` + newline into the two characters `\n`, so bash passed
a bare `n` to argparse and the job died on `unrecognized arguments: n`. It
happened to `reproduce.yml`, was fixed by hand with nothing put in place to
prevent recurrence, and then happened to `liftwing.yml`. The fix is a test
rather than more care, because care had already failed twice.

---

## 3. What was built

| Component | |
|---|---|
| `metrics.py` | PR-AUC, ROC-AUC, Brier, paired margin, over 7d/30d/all-time |
| Segments | four, fixed in the spec before any segmented number existed |
| Calibration | reliability curve, raw **and** population-weighted |
| Populations | `all` at 7 days, `maturity_cohort` at 48 hours, never collapsed |
| `liftwing.py` | sampled at a published 10%, retried, gated-aware |
| `schema.py` | jobs fail by migration name instead of a missing-column traceback |
| `sql/017–019` | metrics, calibration bins, Lift Wing scores and attempts |
| `/metrics`, `/calibration` | published, with exclusions and denominators |
| Status page | live figure beside the backtest figure, labelled as different things |
| Workflows | Metrics (6-hourly), Lift Wing (daily) |

### 3.1 Decisions that will not be obvious later

**Calibration is population-weighted.** The frame keeps 50% of logged-out edits
and 3% of registered ones, so the sample's base rate is roughly four times the
population's. A model calibrated against raw sample frequency would be
calibrated to a population that does not exist and would overstate risk in
production by about that factor.

**Excluding late scores is correct and is not neutral.** Those predictions
concentrate in edits reverted fast, so the exclusion selects on the outcome it
protects. Its own base rate is published beside the count, because "we excluded
4%" and "we excluded 4% that were 60% positive" are different statements about
the same number.

**Bins are equal-width, not quantile.** The question is whether a score of 0.9
means what it says. Quantile edges would move every run, so two runs could not
be compared.

**Where the data cannot support a number, the count is published and the number
is null.** A window with no positives has no PR-AUC. Inventing one is worse
than publishing the gap with its `n` beside it.

**Segments are fixed in the spec.** Four, chosen for what they might falsify.
Ten segments and one looks significant.

---

## 4. Acceptance criteria

| # | | |
|---|---|---|
| D-1 | Live PR-AUC with `n` and CI | **pending data** |
| D-2 | Maturity enforced in the query | met — and the enforcement was wrong twice before it was right |
| D-3 | Immature predictions never counted as negatives | met, regression-tested |
| D-4 | Late scores excluded, own base rate published | met |
| D-5 | Every rate raw and population-weighted | met |
| D-6 | All four segments published every run | met |
| D-7 | Reliability curve and Brier, weighted | met |
| D-8 | Lift Wing comparison, paired, with CI | **pending data** — 55 scores collected, endpoint confirmed open |
| D-9 | Metrics append-only | met, by grant |
| D-10 | Status page distinguishes live from backtest | met |

---

## 5. Production state at time of writing

| | |
|---|---|
| Events | 55,592 |
| Predictions in the register | 25,000 |
| Reproducibility | **496 of 496**, agreement 1.000 |
| Scoring lag | p50 662 min, p90 761, max 799 |
| Scored after outcome observable | 1,133 (4.5%), excluded from every accuracy claim |
| Lift Wing scores collected | 55 |
| Maturity cohort | 608 events, 0 with predictions yet |

The lag figures are rising, and that is the backlog draining rather than the
system falling behind: the median is taken over every prediction ever written,
so it climbs while old events are being scored and will fall once the scorer
reaches the present.

---

## 6. Outstanding

| | |
|---|---|
| **Live figures** | ~16 Aug (cohort), ~20 Aug (all). This summary is revised then, not replaced |
| Reconcile has still never run in production | scheduled 04:23 daily; first run pending |
| `reproduce` will outgrow its timeout | replays every scored prediction in the window; fine at 25,000, not at 280,000 |
| `metrics` bootstrap cost grows with `n` | 27 rows × 2 bootstraps; segments already cut to 500 resamples |
| 12 of 28 features measure at exactly 0 | a decision M5 inherits rather than carries silently |
| Ablation concentration worsening | removing `account_newness` drops the margin to +0.0386, below the KC-2 bar |
| Maturity window still provisional | 48h placeholder; cohort ages ~21 Aug |

The last two are the ones that matter for M5. A self-retraining system whose
entire margin rests on one feature, half of whose feature set is inert, will
retrain into exactly that shape unless the promotion rule says otherwise — and
the promotion rule has to be fixed before any of it runs.
