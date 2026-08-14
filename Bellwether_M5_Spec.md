# Bellwether — M5 Spec

**Self-maintenance.**
Decay and drift detection → automatic retrain → shadow → pre-registered
promotion → automatic rollback → an immutable decision log.

**This is the defensible stopping point.** M0–M5 is a complete project; M6–M8
make it a product. Everything before this milestone built a system that can be
*judged*. M5 is the one that lets it act on the judgement without a human in
the loop, and the only thing that makes that defensible is that the rule was
written down before any model existed.

---

## 1. M5 implements a rule; it does not choose one

`PREREGISTRATION.md` was committed before the first model was trained. It fixes
the promotion conditions, the retrain triggers, the rollback condition, the
statistical test, the minimum sample and its derivation.

**This spec must not restate those numbers as if it were deciding them, and
must not adjust any of them.** Where this document names a threshold it is
quoting, and the pre-registration is the authority. If a threshold turns out to
be wrong, that is an amendment with a date and a reason, made *before* the run
that it would change — never after seeing which way a decision fell.

| # | Requirement |
|---|---|
| M5-FR-1 | Every threshold shall be read from a single module that mirrors `PREREGISTRATION.md`, with the section it comes from named in a comment |
| M5-FR-2 | A test shall assert those constants against the pre-registration text, so editing one without the other fails the build |
| M5-FR-3 | No promotion, rejection or rollback shall depend on a value not present in that module |

M5-FR-2 is the load-bearing one. A pre-registration that the code can silently
drift from is a document, not a commitment.

---

## 2. A conflict M4 introduced, resolved in favour of the pre-registration

`PREREGISTRATION.md` §4 fixes the segments every metric is broken down by:

| Pre-registered (§4) | M4 chose (spec §4) |
|---|---|
| editor class | `sampling_stratum` ✅ same thing |
| edit size band (quartiles) | — |
| hour of day, UTC | — |
| page activity band (quartiles) | — |
| — | `editor_has_history` |
| — | `namespace` |
| — | `scoring_lag_bucket` |

Promotion condition **P-5** says no segment *in §4* may regress in PR-AUC by
more than 0.03. That is the pre-registered list, not M4's.

M4 picked its four for diagnosis, before this conflict was noticed, and they
are good diagnostic segments. They are not the ones the promotion rule refers
to, and using them would quietly change what P-5 tests.

| # | Requirement |
|---|---|
| M5-FR-4 | Promotion decisions shall evaluate P-5 over the **pre-registered** segments of `PREREGISTRATION.md` §4 |
| M5-FR-5 | M4's diagnostic segments shall continue to be published, clearly separated from the decision segments |
| M5-FR-6 | The three missing pre-registered segments — size band, hour of day, page activity band — shall be implemented, with quartile boundaries taken from the **training window** and frozen with the model version |

M5-FR-6's boundary rule matters. Quartiles recomputed per evaluation window
would move under the metric, so a segment could regress because the bands
shifted rather than because the model did.

---

## 3. Shadow scoring

M3 put `role` on `register.predictions` from the outset precisely so this needs
no migration on the evidential table.

| # | Requirement |
|---|---|
| M5-FR-7 | A challenger shall score every event the champion scores, in the same run, from the same state, and write `role = 'challenger'` |
| M5-FR-8 | Challenger scores shall never be served, and shall be excluded from every published headline figure |
| M5-FR-9 | A challenger shall be scored by the same code path as the champion — one scorer, two model versions, never a second implementation |
| M5-FR-10 | If the challenger fails to score an event the champion scored, that event shall be excluded from the paired comparison rather than counted as a loss |

M5-FR-10 exists because the alternative is a challenger that looks worse
whenever it errors, which is a way of hiding an unstable model behind a
mediocre metric.

---

## 4. Triggers

Quoting `PREREGISTRATION.md` §8:

| Trigger | Condition |
|---|---|
| Decay | rolling 7-day PR-AUC below the champion's registered baseline by more than **0.03**, on **3 consecutive daily windows** |
| Input drift | **PSI > 0.20** on any monitored feature or on the score distribution, on **3 consecutive daily windows** |
| Floor | **7 days** since the last training run |

| # | Requirement |
|---|---|
| M5-FR-11 | Triggers shall be evaluated daily and every evaluation recorded, including the ones that fire nothing |
| M5-FR-12 | Three *consecutive* windows shall mean three consecutive daily evaluations, and a gap shall reset the count rather than be skipped over |
| M5-FR-13 | PSI shall be computed against the champion's **training** distribution, with bin edges frozen at training time |
| M5-FR-14 | A trigger firing shall be recorded even when a retrain is already running |

M5-FR-12 is the one that will be got wrong if it is not stated. If a daily
evaluation is missed — GitHub's cron is best-effort — treating "the last three
evaluations" as "three consecutive days" would let a trigger fire on evidence
spanning a week.

M5-FR-13 matters because PSI against a rolling recent window measures whether
today resembles yesterday, which is not the question. The question is whether
today resembles what the model was fitted to.

---

## 5. Retraining

| # | Requirement |
|---|---|
| M5-FR-15 | A fired trigger shall retrain automatically, with hyperparameters fixed as in M3 — no search, ever |
| M5-FR-16 | The training window shall be a stated function of the trigger date, not a choice made per run |
| M5-FR-17 | A retrained model shall enter **shadow**, never production, regardless of its offline metrics |
| M5-FR-18 | The knowability guard shall run before training, as in M3, and a failure shall abort rather than warn |
| M5-FR-19 | Only one challenger shall exist at a time; a new retrain while a challenger is in shadow shall replace it and reset P-3 and P-4 |

M5-FR-17 is the whole architecture in one line. Offline metrics have never once
in this project agreed with the live ones — in-sample 0.729 against
out-of-sample 0.256 — and a model that promotes itself on the strength of them
is a model that promotes itself on memorisation.

M5-FR-19's reset is deliberate and expensive: a challenger replaced after five
days starts its seven-day clock again. The alternative is accumulating shadow
evidence across models, which is not evidence about either.

---

## 6. Promotion

Quoting `PREREGISTRATION.md` §5. All five must hold:

| # | Condition |
|---|---|
| P-1 | PR-AUC exceeds the champion's by at least **0.02 absolute**, on the same matured events |
| P-2 | The paired bootstrap 95% interval for that difference **excludes zero** |
| P-3 | At least **2,500 matured positives** accumulated in shadow |
| P-4 | At least **7 days** of wall-clock shadow elapsed |
| P-5 | ECE no worse by more than **0.02**, and no §4 segment regresses in PR-AUC by more than **0.03** |

| # | Requirement |
|---|---|
| M5-FR-20 | Promotion shall be evaluated on **matured** predictions only, by the M4 rule — elapsed time since the edit, and the outcome determined |
| M5-FR-21 | Late-scored predictions shall be excluded from the comparison, as everywhere else |
| M5-FR-22 | A rejection shall be recorded with the **same evidence** a promotion would carry |
| M5-FR-23 | ECE shall be computed from the M4 calibration bins, population-weighted |
| M5-FR-24 | `registry.champion()` shall stop meaning "most recently registered" and start meaning "the model this decision log promoted" |

M5-FR-24 replaces the M3 placeholder, which was named as one in `sql/013` when
it was written.

M5-FR-22 is what makes the log worth keeping. A log containing only promotions
answers "what changed" and cannot answer "what was considered and refused",
which is the more interesting question and the one that shows the rule binding.

---

## 7. Rollback

`PREREGISTRATION.md` §9: if a newly promoted champion's rolling 7-day PR-AUC
falls below the previous champion's registered level by more than **0.02**
within **14 days**, the previous champion is automatically restored.

| # | Requirement |
|---|---|
| M5-FR-25 | Rollback shall be automatic and shall require no human action |
| M5-FR-26 | A rollback shall be an ordinary decision row, not an error state, and shall be published as prominently as a promotion |
| M5-FR-27 | A rolled-back model shall not be re-promoted by the same evidence that promoted it the first time |
| M5-FR-28 | The 14-day window shall run from the promotion decision, not from the model's training date |

M5-FR-27 prevents the obvious oscillation: promote, roll back, and promote
again on the identical shadow record. A model that has been rolled back needs
new evidence, not the old evidence re-read.

---

## 8. The decision log

| # | Requirement |
|---|---|
| M5-FR-29 | `decide.model_decisions` shall record every promotion, rejection and rollback, append-only **by grant** |
| M5-FR-30 | Each row shall carry the trigger, the challenger and champion versions, every P-condition's measured value and verdict, the sample sizes, and the code commit |
| M5-FR-31 | A decision shall be reconstructible from the row alone, without database access to anything else |
| M5-FR-32 | The log shall be published at `/decisions`, whole, including rejections |
| M5-FR-33 | Decisions shall join the monthly seal manifest |

M5-FR-31 is the standard the rest of the project has been held to: the metric
card, the artifact digest in git, the seal in public history. A decision that
requires the database to interpret is a decision only its owner can check.

---

## 9. Storage

| | |
|---|---|
| Challenger predictions | doubles `register.predictions` while a challenger exists — ~1.1 MB/day |
| Trigger evaluations | one row/day + per-feature PSI, negligible |
| Decisions | a handful of rows, ever |

| # | Requirement |
|---|---|
| M5-FR-34 | Challenger predictions shall be pruned on the M1 mechanism once the challenger is resolved, after sealing |
| M5-FR-35 | Decisions shall never be pruned |

---

## 10. Acceptance criteria

| # | |
|---|---|
| D-1 | A challenger scores in shadow alongside the champion, same run, same state |
| D-2 | Each of the three triggers fires in a test that constructs its exact condition |
| D-3 | A trigger firing produces a retrained challenger with no human action |
| D-4 | Each of P-1 to P-5 is demonstrated **blocking** a promotion on its own |
| D-5 | A rejection is recorded with the same evidence a promotion would carry |
| D-6 | A promotion changes what `registry.champion()` returns, and the scorer follows it |
| D-7 | A rollback restores the previous champion automatically, demonstrated |
| D-8 | The decision log is append-only, proven on the production server |
| D-9 | `/decisions` serves every decision including rejections |
| D-10 | The pre-registered constants match the document, asserted by a test |

D-4 is the criterion that matters. Five conditions of which four are never
exercised is one condition and four comments.

---

## 11. Timeline, stated in advance

The machinery can be built and tested immediately. A **real** promotion decision
cannot happen before:

- **P-4**: 7 days of wall-clock shadow, from the first challenger
- **P-3**: 2,500 matured positives — at the observed ~5.8% base rate, roughly
  43,000 matured predictions
- **Maturity**: 7 days from each edit, per M4 §2.2

Earliest realistic first decision: **around 2026-08-28**. That is a
consequence of the pre-registered sample requirement, not of the
implementation, and shortening it would mean weakening P-3 or P-4 after seeing
how long they take — exactly what the pre-registration forbids.

**M5 is complete when the machinery runs, records decisions, and can be shown
to reject.** It is not complete only if a promotion happens; a system that
correctly refuses to promote a worse model is the system working.

---

## 12. What M5 must not become

**A system that promotes.** The success condition is a *correct* decision, and
on current evidence the most likely correct decision is rejection. A milestone
that is only satisfied by a promotion will produce one.

**A place where the rule gets adjusted.** Every threshold here is quoted. The
moment one is tuned because a decision came out inconveniently, every number
this project has published becomes a number chosen after the fact.

**Automation without a record.** An unattended system that cannot explain a
decision after the fact is worse than a manual one, because nobody was watching
when it happened.

**A reason to stop measuring.** M4's jobs keep running. A self-maintaining
system that stops grading itself is just a system that changes on its own.
