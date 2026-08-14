# Bellwether — M7 Spec

**The human feedback loop.**
Reviewer labels entering retraining as a weighted signal, and a study of how
good a proxy "was reverted" is for "was a bad edit".

SRS FR-47 to FR-49, and BQ-8.

---

## 1. What M7 is for

M6 collects human judgements and keeps them apart from everything else. M7 is
where they start mattering: they enter training, and they are used to measure
the assumption the entire project rests on.

**That assumption has never been checked.** Every label in this system says
*this edit was reverted*. Every metric, every promotion condition, the kill
criterion — all of it is built on reverting standing in for *this edit was
bad*. BQ-8 asks how good that stand-in is, and until M7 nobody has looked.

---

## 2. The problem this milestone has to solve first

**A queue ranked by the model, labelled by a human, fed back into the model, is
a machine for confirming what the model already believes.**

The queue shows the highest-scored edits. A reviewer labels what they see. Those
labels enter training. The next model learns from a sample drawn almost entirely
from the top of the previous model's ranking — and receives *almost no signal
about the items it scored low*.

Its false negatives are exactly the errors that matter, and they are exactly the
ones this loop can never show it. The model would get more confident, its
measured agreement with reviewers would improve, and its actual recall could
fall the whole time without anything in the system noticing.

This is not a hypothetical failure of an unusual design. It is the *default*
behaviour of the design SRS §1 describes, and building it as described would
produce it.

| # | Requirement |
|---|---|
| M7-FR-1 | A fraction of queue items shall be selected **at random**, not by rank, and labelled alongside the ranked ones |
| M7-FR-2 | Every human label shall record which slice it came from — ranked or random — and that field shall never be inferred afterwards |
| M7-FR-3 | The random slice shall be drawn from the same maturity-eligible population as the ranked slice, so the two differ only in selection |
| M7-FR-4 | Agreement figures (FR-49) shall be computed on the **random slice alone**, and a figure computed over the ranked slice shall be published only when labelled as conditioned on the model's own ranking |
| M7-FR-5 | The reviewer shall not be told which slice an item came from |

M7-FR-5 matters more than it looks. A reviewer who knows an item was randomly
drawn knows it is probably fine, and will judge it differently. The slice has to
be invisible at the point of judgement or it stops being a control.

M7-FR-4 is the one that makes BQ-8 answerable at all. κ computed over the
ranked slice measures agreement *among edits the model already flagged*, which
is not the question anyone is asking.

---

## 3. What M6 handed over

| # | Inherited | Consequence |
|---|---|---|
| I-1 | `app.human_labels` records verdict, confidence, champion version and the score shown | The score shown is already there, which is what makes propensity recoverable |
| I-2 | Labels are `bad_edit` / `good_edit` / `unsure`; the proxy is binary | A mapping is needed, and `unsure` cannot simply be dropped |
| I-3 | One reviewer exists | Inter-rater reliability is not computable, and "human judgement" means one person |
| I-4 | Human labels never touch `outcome.labels` | Keep it that way; FR-47 is already satisfied structurally |
| I-5 | Maturity is 7 days | A label collected today cannot be compared to an outcome for a week |

---

## 4. Human labels in training (FR-48)

| # | Requirement |
|---|---|
| M7-FR-6 | Human labels shall enter training as an additional weighted signal, never as a replacement for the revert label |
| M7-FR-7 | The weight shall be **fixed in this spec before any model is trained with it**, and recorded in the model registry entry for every model that used it |
| M7-FR-8 | A model trained with human labels shall record how many it used, from which slices, and over what window |
| M7-FR-9 | Human labels shall not enter the evaluation set — they change what a model learns, never what it is measured against |
| M7-FR-10 | Training with human labels shall be reproducible: the same window and the same labels produce the same model |

**The weight is 3.0**, fixed here. One human judgement counts as three
automatically-labelled events.

That number is a choice and it is stated rather than derived, because deriving
it would mean trying several and keeping the one that scored best — a
hyperparameter selected on the evaluation it is about to be judged by, which is
what M3 forbade and M5 pre-registered against. If it turns out to be wrong, it
is an amendment with a date, made before the run it would change.

M7-FR-9 is the leak guard for this milestone. Human labels are collected on the
queue, the queue is drawn from scored events, and scored events are what the
register measures. Letting a human label into both sides would mean a model
graded partly on data it was fitted to — the same contamination M3 found in the
scorer, arriving by a new route.

---

## 5. The agreement study (FR-49, BQ-8)

| # | Requirement |
|---|---|
| M7-FR-11 | Cohen's κ between human verdict and the revert proxy, on matured events from the random slice |
| M7-FR-12 | The full confusion matrix, published whatever it shows |
| M7-FR-13 | `unsure` shall be reported as its own rate, never silently dropped |
| M7-FR-14 | Every figure shall carry its `n`, and κ shall not be published below **n = 100** matured random-slice labels |
| M7-FR-15 | The number of distinct reviewers shall be published beside every agreement figure |

### 5.1 `unsure` is not missing data

Dropping `unsure` is the obvious move and it is wrong. Those are the ambiguous
cases — the ones where the proxy is most likely to disagree with a human, which
is precisely what BQ-8 is about. Excluding them selects on the outcome being
studied.

So the rate is published, and κ is reported both with them excluded and with
them mapped to `good_edit`, labelled as two different estimates of two
different things.

### 5.2 One reviewer is not "human judgement"

With a single reviewer, κ measures agreement between the proxy and *one
person*. Inter-rater reliability cannot be computed at all, so there is no way
to tell how much of any disagreement is the proxy being wrong and how much is
that reviewer.

| # | Requirement |
|---|---|
| M7-FR-16 | Below **two** reviewers, agreement figures shall be labelled as one person's judgement rather than as a property of the proxy |
| M7-FR-17 | Where two or more reviewers have judged overlapping events, inter-rater κ shall be published beside the proxy κ |

M7-FR-17 will very likely produce nothing for a long time. It is specified now
so that the absence is a known gap rather than an unasked question.

---

## 6. What M7 cannot answer yet, stated in advance

| | |
|---|---|
| Human labels collected | ~0 at the time of writing |
| κ needs | 100 matured random-slice labels, from ≥2 reviewers to mean what BQ-8 asks |
| Maturity | 7 days from each labelled edit |
| Realistic first κ | weeks away, and only if labelling actually happens |

**M7 is complete when the machinery works and the study can be run**, not when
κ has a value. A milestone that required a number would produce one — from the
ranked slice, from one reviewer, over thirty events — and that number would be
worse than no number, because it would be quoted.

---

## 7. Storage

| | |
|---|---|
| Human labels | bounded by how much a human reads; negligible |
| Agreement runs | one row per computation, append-only |
| Random slice | a flag on existing rows, no new volume |

---

## 8. Acceptance criteria

| # | |
|---|---|
| D-1 | A random slice appears in the queue, indistinguishable to the reviewer |
| D-2 | Every human label records its slice, and the field cannot be back-filled |
| D-3 | Training with human labels records the weight, the count and the slices in the registry |
| D-4 | A model trained with human labels is reproducible from the same inputs |
| D-5 | Human-labelled events are demonstrably absent from the evaluation set |
| D-6 | κ, the confusion matrix and the `unsure` rate are computed on demand and published with `n` |
| D-7 | κ is refused below the threshold, and the refusal says why |
| D-8 | The reviewer count is published beside every agreement figure |
| D-9 | A figure computed over the ranked slice is labelled as conditioned on the ranking |

D-7 is the one that will be exercised first, and for a long time it may be the
only one that is.

---

## 9. What M7 must not become

**A loop that confirms the model.** The random slice is not a refinement; it is
the only thing standing between this design and a system that gets more
confident while getting worse.

**A κ that gets quoted.** Computed over the ranked slice, from one reviewer, on
a handful of events, it would be a number with a Greek letter on it and no
meaning. The thresholds exist to stop it existing.

**A reason to change the proxy.** If κ comes out low, that is a finding about
the proxy, published. It is not permission to relabel history, reweight past
metrics, or reinterpret KC-2 — every number this project has published was
computed against the proxy as defined, and it stays that way.

**A replacement for the automatic label.** Human labels are additional signal at
a fixed weight. The revert proxy remains the target. A system that learned
primarily from one reviewer would be a model of that reviewer.
