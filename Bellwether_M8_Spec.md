# Bellwether — M8 Spec

**Communication.**
A methods document, a decision memo, a README, and an alert that fires when the
system stops being able to keep its own promises.

SRS FR-50 to FR-52.

---

## 1. What M8 is for

Everything the previous eight milestones built is worth exactly what somebody
else can read out of it. A system that grades itself honestly and cannot explain
what it found is a system nobody has reason to believe.

This is also the milestone with the strongest pull towards dishonesty, and it is
worth naming that before writing a word. The audience is hiring managers. The
findings are mixed — a model that clears its kill criterion on one feature, a
live figure that does not exist yet, a benchmark against Wikimedia that is
expected to lose. Every incentive points at softening those.

**The project's entire claim is that it did not.** A decision memo that oversells
would retroactively make eight milestones of leak guards and pre-registration
into decoration.

---

## 2. What the previous milestones handed over

| # | Inherited | Consequence for M8 |
|---|---|---|
| I-1 | Most live figures do not exist yet | The memo describes a system, and states plainly which of its numbers are pending |
| I-2 | The KC-2 margin rests on one feature | That belongs in the memo, not only in a spec appendix |
| I-3 | SRS §6.4 predicted Lift Wing wins | Published whichever way it lands, and the prediction is quoted |
| I-4 | Four SRS requirements have been amended | Each with a date and a reason; the memo says so rather than presenting v1.0 as delivered |
| I-5 | Failures were found by tests, not by monitoring | Nothing currently tells anyone the pipeline stopped |

---

## 3. METHODS.md (FR-51)

| # | Requirement |
|---|---|
| M8-FR-1 | The sampling frame, its rates, and the weight correction, with the reason case-control was chosen |
| M8-FR-2 | Maturity estimation: the survival method, the checkpoint grid, and why the metric window is seven days rather than M2's 48 hours |
| M8-FR-3 | Every statistical procedure: PR-AUC, the paired bootstrap, PSI, Cohen's κ, ECE, and the calibration binning |
| M8-FR-4 | Point-in-time correctness: the fold order, the knowability guard, and the detection-time rule |
| M8-FR-5 | Every exclusion, with the direction of the bias it introduces |
| M8-FR-6 | Enough detail that a reader could recompute a published number from the database without asking a question |

M8-FR-5 is the one that distinguishes a methods document from a description.
Every exclusion in this project is defensible and none of them are neutral —
late-scored predictions concentrate in fast reverts, `unsure` concentrates in
ambiguous cases, immature events skew toward the recent. A methods document that
lists exclusions without their direction has explained nothing.

---

## 4. DECISION_MEMO.md (FR-52)

| # | Requirement |
|---|---|
| M8-FR-7 | At most two pages |
| M8-FR-8 | Readable with no technical background: no metric names in the first half, no jargon left undefined |
| M8-FR-9 | It shall state what the system **found**, including the findings that are unflattering |
| M8-FR-10 | Every number shall carry its sample size and whether it is provisional |
| M8-FR-11 | It shall state what is **not yet known**, rather than omitting it |
| M8-FR-12 | It shall not claim the model is good |

M8-FR-12 is deliberate and absolute. The deliverable was never a good model; it
is a system that can be trusted about a model. The most honest summary available
today is *this thing measures itself correctly, and what it has measured so far
is modest* — and that is a stronger claim than a PR-AUC, because almost nobody
can support it.

---

## 5. README (the front door)

| # | Requirement |
|---|---|
| M8-FR-13 | What it is, what it does, and what it found — above the fold |
| M8-FR-14 | Links to the live pages and to the documents, so nothing has to be run to be checked |
| M8-FR-15 | The architecture and the cost, since zero-cost is a design constraint rather than a footnote |
| M8-FR-16 | An honest limitations section, not buried |

---

## 6. Alerting

Every failure this project found was found by a test or by a person looking.
Nothing tells anybody when the pipeline stops.

That gap has already cost something: the API served a three-commit-old build for
hours while `/health` reported `ok`, because the old code was fine.

| # | Requirement |
|---|---|
| M8-FR-17 | A scheduled watchdog shall fail loudly when ingestion, labelling or scoring has stopped |
| M8-FR-18 | It shall fail when the running build does not match the repository — the gap that hid the failed deploys |
| M8-FR-19 | It shall fail when a guarantee is violated: schema behind, reproducibility below 100%, or state divergence |
| M8-FR-20 | The alert shall be a **failing GitHub Actions run**, not email — email costs money or depends on a free tier that can be withdrawn (NFR-1) |
| M8-FR-21 | It shall distinguish "has not run yet" from "has stopped", and never alert on the first |

M8-FR-20 is the free-tier answer and it is honest about its limits: a failing
workflow notifies whoever is watching the repository, which is one person, by
email that GitHub sends. That is not an on-call system and the README should not
imply it is.

M8-FR-21 matters because this project has spent eight milestones distinguishing
*nobody has looked* from *we looked and found nothing*, and an alert that cannot
tell them apart would undo that at the last step.

---

## 7. Acceptance criteria

| # | |
|---|---|
| D-1 | METHODS.md documents every procedure that produces a published number |
| D-2 | Every exclusion appears with the direction of its bias |
| D-3 | DECISION_MEMO.md is at most two pages and names no metric in its first half |
| D-4 | The memo states at least three findings that do not flatter the project |
| D-5 | The memo states what is not yet known |
| D-6 | The README links every live page and every document |
| D-7 | The watchdog fails on a stale pipeline, demonstrated |
| D-8 | The watchdog fails on a build/repository mismatch, demonstrated |
| D-9 | The watchdog is silent on a system that has never run, demonstrated |
| D-10 | Every amended SRS requirement is listed with its date and reason |

D-4 is the criterion that keeps the memo honest. Three is an arbitrary number
and its arbitrariness is the point: a memo with none is a brochure, and the
threshold has to be met before anyone reads whether it was.

---

## 8. What M8 must not become

**A sales document.** The audience is people hiring, and the temptation is to
present a mixed result as a good one. Every guard in this project was built to
stop exactly that happening to a metric; it would be absurd to do it in prose at
the end.

**A claim that the model is good.** It clears its kill criterion. The margin
rests substantially on one feature. Wikimedia's model is expected to beat it.
All three are true and all three go in.

**A promise of monitoring that does not exist.** A failing workflow is not
alerting. It is a red mark on a page that one person may or may not look at, and
the README says so.

**A rewrite of history.** Four SRS requirements were amended, each before the
work it governed. The memo lists them. A v1.0 presented as delivered intact
would be the one dishonest thing in a project built to prevent exactly that.
