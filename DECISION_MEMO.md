# Bellwether — Decision Memo

**What this system found.**
Written 2026-08-15. Two pages. Readable with no technical background.

---

## What was built

Anyone can edit Wikipedia. A small group of volunteers reads incoming edits
looking for damage, and there are far more edits than there are people to read
them. Bellwether watches those edits as they arrive, guesses which are likely to
be undone, and puts them in a queue worst-first.

The guessing is not the point. The point is that **a system like this quietly
stops working** — the people it watches change how they behave, and the model
keeps answering with the same confidence it always had. Almost every deployed
model is tested once before it ships and trusted forever after.

So this one grades itself continuously, decides for itself when it has decayed,
builds and tests a replacement, and promotes or refuses that replacement by a
rule written down before either model existed. Nobody approves any of it, and
every decision is published with the evidence that produced it.

---

## What it found

### 1. The system works. The model is ordinary.

It has run unattended since 10 August: 65,667 edits collected, 48,064 forecasts
committed before their outcomes existed, 40,350 outcome checks, and a complete
public record of every decision.

The model itself is modest. On the offline test it ranks edits better than the
simple rule it must beat — but by a margin that depends heavily on one input
(below). **This memo does not claim the model is good.** The deliverable was
never a good model; it is a system that can be trusted about a model.

### 2. Nearly all of the model's advantage comes from one thing

The model must beat a trivial rule — *treat every logged-out editor as
suspicious* — by a set margin. It does, comfortably.

**Remove the single input describing how new an account is, and most of that
advantage disappears.** The margin falls from 0.107 to 0.039, below the 0.050
required. On this evidence the model is largely a sophisticated way of noticing
that new accounts are riskier.

That was measured, published, and **not acted on** — the criterion asked whether
a leak-free model beats the simple rule by the margin, and it does. Adding "and
it must survive having its best feature removed" after seeing the result would be
changing the test to suit the answer, which is the one thing this project was
built to prevent.

### 3. Twelve of twenty-eight inputs contribute nothing measurable

Their measured importance is exactly zero. They are not removed, because removing
them after seeing which ones scored badly is the same mistake as above in a
different coat. They are recorded.

### 4. Its own guarantees caught its own mistakes — repeatedly

The value of the machinery is not theoretical. Over nine milestones it caught,
among others:

- A published revert rate of 22.04% that should have been 38.21% — caused by the
  very append-only guarantee that makes the record trustworthy.
- A first accuracy run where **every one of 178 events was a revert**, because
  the maturity rule let positives in immediately while negatives waited.
- A queue reporting an empty list over 45,000 predictions, with a message saying
  the scorer had not run.
- Three deployments that failed silently while the health check reported
  everything fine, because the previous version was still answering.

Each was found because something was built to look for it. None announced
itself.

### 5. What is not known yet

Stated plainly, because the alternative is implying otherwise:

| Question | Status |
|---|---|
| **How accurate is it on live data?** | **No figure yet.** Nothing has matured — an outcome is only settled after seven days, and the register is five days old |
| Is it better than Wikimedia's own model? | Not yet compared. 246 of their scores collected; the comparison needs matured outcomes. **It is expected to lose,** and that expectation was recorded before any model existed |
| Does "was undone" mean "was bad"? | Being measured. No answer yet — it needs 100 judged edits from at least two reviewers |
| Has it replaced its own model yet? | No. Nothing has been promoted, rejected or rolled back. The machinery runs daily and records that it found nothing to do |

The live accuracy figure is the headline number this project exists to produce,
and **it does not exist yet.** Publishing a provisional one from an incomplete
window would be the exact failure the whole design refuses.

---

## The one assumption everything rests on

Every label in this system says *this edit was undone*. Every figure treats that
as meaning *this edit was bad*.

That is not quite true. Good edits get undone by mistake; bad ones survive
because nobody noticed. If the substitution is poor, every number here is
measuring something other than what it claims.

Rather than assume it away, the system is measuring it: reviewers judge edits
**without being shown the model's opinion**, a fifth of the queue is drawn at
random rather than by risk so the sample is not just what the model already
flagged, and their verdicts are compared against what actually happened. That
study is running and does not have an answer yet. It will be published whichever
way it comes out.

---

## What changed along the way

Four requirements from the original specification were amended. Each was changed
**before** the work it governed, with a date and a reason, and none after seeing
a result:

| # | Change | Why |
|---|---|---|
| FR-38 | Email sign-in → administrator-issued accounts | Sending email needs a paid service or a free tier that can be withdrawn |
| FR-40 | Queue strictly rank-ordered → contents selected by rank, order shuffled, score hidden until judged | A reviewer shown the score is agreeing with a number rather than judging the edit |
| FR-37 | Freeze halts promotion but **not** rollback | A freeze is usually set because something looks wrong, which is when rollback matters most |
| §6.4 | Reproducibility claim narrowed to the serving model | A correctness fix changed how state is built, so earlier predictions genuinely cannot be re-derived |

A specification presented as delivered intact would have been the one dishonest
thing in a project built to prevent exactly that.

---

## Honest limits

- **It runs on free infrastructure.** The server sleeps when idle; the first page
  load can take a minute.
- **It reads a sample, not everything** — 50% of logged-out edits, 3% of
  registered ones — corrected by weighting in every figure.
- **Monitoring is one red mark on one page.** A failing scheduled job notifies
  one person. That is not an on-call system and is not presented as one.
- **One reviewer exists.** Until there are two, human-judgement figures describe
  one person's opinion, not a property of anything.
- **It never edits Wikipedia.** It reads, ranks and reports.

---

## The claim, stated exactly

Not *this model is accurate*. It is:

> Here is a system that commits its forecasts before the answers exist, grades
> itself only on outcomes that have actually settled, decides by rules written
> down in advance, publishes what it refused as prominently as what it did, and
> has repeatedly caught itself being wrong — including in ways that were nobody's
> fault but its author's.

Whether that is worth more than a higher accuracy number is a judgement for the
reader. The numbers, the rules, the refusals and the code are all public so that
judgement can be made without taking anything here on trust.
