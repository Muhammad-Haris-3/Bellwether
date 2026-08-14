# Bellwether — M3 Summary

**The prediction register.**
Closed 2026-08-14. 209 tests. 20 of 20 requirements, 9 of 9 acceptance criteria.

M3 is the milestone where the project starts making claims that can be checked
against it. Everything before this built the evidence; this is the first
milestone that commits a forecast to a table nobody can edit, before the answer
exists, and then hands over the tools to catch itself being wrong.

---

## 1. What is now true

| | |
|---|---|
| Predictions written | 10,000, all before their outcome was knowable to this system |
| Register | append-only **by grant** — the writer role holds `INSERT` and nothing else |
| Backdating | structurally impossible: `CHECK (scored_at >= event_ts)` |
| Champions | 2, both in git with their metric cards and SHA-256 |
| Artifact verification | digest checked **before** load; a mismatch refuses to score |
| Reproducibility | 496 of 496 sampled predictions re-derive exactly — hash and score |
| Scoring lag | published as a distribution: p50 580 min, p90 639, max 657 |

The lag figures are a backfill artifact and say so on the endpoint. The first
champion was registered 36 minutes after the last ingest run, so the scorer's
opening runs drained a backlog that had accumulated while no model existed. It
falls as real-time scores accumulate.

---

## 2. Findings

Four things went wrong, and three of them were found by machinery built in this
milestone rather than by reading the code. That ratio is the point of M3.

### 2.1 The outcome guard was counting a seventh of what it should

`outcome_already_observable` (M3-FR-10) consulted only `outcome.revert_events`,
which is built from reverting edits parsed out of edit summaries. Most outcomes
never take that path — they arrive as `mw-reverted` tags, land in
`outcome.labels`, and produce no `revert_events` row at all. Production held 601
revert_events against 2,419 labels.

| | count | share |
|---|---|---|
| Flagged by the scorer at write time | 15 | 0.3% |
| Recomputed against everything known | **207** | **4.1%** |

Found because 15-in-5,000 looked implausibly clean for edits averaging ten hours
old. Nothing failed; the number was simply wrong, and it was wrong in the
direction that flatters.

Two limbs are now checked, because they are different failures: the revert
**happened** before scoring (information could have reached the features through
state), or we **already held the answer** (the row is a lookup wearing a score).

The 5,000 predictions already written carry the understated flag and cannot be
corrected — `register.predictions` has no `UPDATE` grant, which is the point of
it. So `/register` recomputes the figure on read against `scored_at` rather than
`now()`, and publishes the scorer's own flag beside it. A stored flag that can
only ever understate is not a number to publish alone.

### 2.2 The replay encoded a leak the knowability guard cannot see

The state repair job was built to catch a missing increment, and did. Underneath
it was something worse.

`state.replay` folded reverts at `revert_ts` — the moment a revert happened on
Wikipedia. This system does not *learn* of a revert until MediaWiki's deferred
tagging job runs and the label pass finds it, thirty minutes or more later. So
training histories were built from knowledge production could never have had.

The knowability guard is structurally blind to this. It proves no **feature**
depends on the future of the event it describes. The **state** did.

Both sides now fold at discovery time, and `evaluate.py` — which had its own copy
of the fold, with its own `revert_ts`, and is the path that actually built the
training matrix — imports the single definition. `state.py`'s docstring claimed
one shared implementation made train/serve skew impossible. The *function* was
shared. The SQL feeding it was not.

**And it moved nothing.** `editor_edits_reverted` and `page_edits_reverted`, the
two features the leak touched, both measure at exactly **0.000** permutation
importance. The fix bought correctness, not accuracy. It was still right to make
— "it happens not to matter for the current champion" is not knowable before
measuring, and does not stay true when M5 retrains.

### 2.3 The scorer would have scored its own training data

The lookback window and the training window are set independently and nothing
stopped them overlapping. This champion scores **0.729 in-sample against 0.256
out-of-sample**, so writing memorised edits into the register that measures
out-of-sample behaviour would not have been subtle.

It had not happened, but only because an ingestion gap happens to sit between
the two windows. Luck, not design; the next champion trained on a recent window
walks straight into it. Now refused by the scorer, with the training window
travelling alongside the champion for that reason.

### 2.4 Reproducibility: two wrong answers, and why they were worth having

The reproducibility job reported 98.99% → 61.90% → 100%. The middle number is
the useful one.

| Attempt | Replay definition | Agreement |
|---|---|---|
| 1 | 2-day window over `rc_events` | 98.99% — 5 unexplained |
| 2 | 30-day window over `rc_events` | **61.90%** — worse |
| 3 | the scored set, in scoring order | **100.00%** |

Attempt 2 was a deliberate hypothesis: that failures came from editors whose
state predated the replay window. Widening the window made it *worse*, which
killed the hypothesis and pointed at the real mechanism.

The scorer's state is not built from events in a time window. It is built from
the events **it has scored**, and its lookback bounds that set — the 08-10
backfill sits outside the lookback and was never folded at all. A two-day replay
excluded it by accident and nearly matched; a thirty-day replay included it and
invented history. The wider it reached, the more it made up.

The replay is now `register.predictions` joined to its events, ordered by
`(scored_at, event_ts, revid)`. Each run stamps one `scored_at` across its batch
and folds within the batch in `event_ts` order, so that tuple **is** the fold
order rather than an approximation of it. Reverts fold on the same principle:
when `apply_reverts` wrote them to the table the scorer reads.

Both wrong answers came from the code contradicting a prediction written down in
advance. That is the only reason the third is trustworthy.

---

## 3. What was built

| Component | |
|---|---|
| `score.py` | scores in `event_ts` order, folds after emitting, refuses a mismatched artifact |
| `registry.py` | SHA-256 verified before load; champion carries its training window |
| `train.py` | fixed hyperparameters, no tuning, artifact + card committed to git |
| `state.apply_reverts` | folds discovered reverts into persisted counters, exactly once, by primary key |
| `reconcile.py` | replays and compares; fails loudly, never self-heals |
| `reproduce.py` | re-derives a deterministic 5% sample; hash and score must match |
| `sql/011–016` | register, pipeline state, model registry, applied reverts, reproductions |
| Workflows | Reconcile (04:23 daily), Reproduce (05:19 daily), scoring wired into Ingest |

### 3.1 The repair job refuses to repair

M3-FR-13, and it is the requirement rather than a limitation. `reconcile` reports
divergence and exits non-zero; `--repair` exists and is never scheduled. A job
that quietly corrects drift removes the only signal that something is producing
it, and the next morning everything looks fine again.

Its comparison is scoped to editors and pages whose persisted `first_seen_utc`
falls inside the window — those, and only those, had their whole history inside
what the replay can see. Judging older keys would make the agreement rate a
function of how far back the job happens to look rather than of whether anything
is wrong.

### 3.2 Deliberate limits, published rather than glossed

**Only the feature hash is stored, not the vector.** So a reproduction failure
says something differs without saying what. Storing 28 floats a row would fix it
and cost roughly 60 MB a month against a 512 MB budget, which this project
cannot afford. The limitation is stated in `/register`.

**Out-of-scope is counted, never dropped.** A job that silently discards what it
cannot verify reports a clean agreement rate over a shrinking denominator, which
looks better every time it gets worse. `state_predates_window` is published as
its own count with the checkable denominator beside it.

---

## 4. Acceptance criteria

| # | | Evidence |
|---|---|---|
| D-1 | one champion score per sampled event | idempotent by constraint; re-run scores 0 |
| D-2 | writer proven unable to update or delete a prediction | bootstrap probes, on the production server |
| D-3 | backdated score rejected | `tests/test_register.py` |
| D-4 | lag published as a distribution | `/register` |
| D-5 | late scores flagged and excluded | `/register`, recomputed on read |
| D-6 | persisted state matches a replay, job fails on divergence | `reconcile.py`, 5 tests |
| D-7 | artifact in git with its hash, mismatch refused | `models/`, `registry.verify` |
| D-8 | historical predictions recompute exactly | **496 of 496** |
| D-9 | drift-stable feature replaces `log_user_id`, margin and ablation published | §2.1 of the M3 spec, `/kc2` |

### 4.1 KC-2 was re-earned, not carried over

M2's verdict was measured with reverts folded at `revert_ts` — the state M3
changed. Re-running was not optional:

| | M2 | M3 |
|---|---|---|
| Margin | +0.1165 | **+0.1072** |
| CI | [+0.0882, +0.1506] | [+0.0794, +0.1394] |
| Required | +0.0500 | +0.0500 |
| Null check | 0.99× base rate | 0.98× base rate |

Still clears, whole interval above the line. The M2 summary now points at this
rather than restating its numbers — that the first answer came from code with a
leak in it is the more useful fact.

**The ablation got worse.** Removing `account_newness` drops the margin to
**+0.0386**, down from +0.0441 and further below the bar. KC-2 clears by the rule
as written, and adding "must survive ablation" after seeing the result is exactly
the post-hoc criterion change `PREREGISTRATION.md` forbids. But the concentration
is worsening, not stabilising, and M4 inherits it.

---

## 5. Outstanding

| | |
|---|---|
| Reconcile has never run in production | scheduled 04:23 daily; first run is tonight, and may report red until the scoring backlog drains |
| Reproduce will outgrow its timeout | replays every scored prediction in the window — fine at 10,000, ~280,000 at steady state, fetched with all feature columns into memory |
| 12 of 28 features measure at exactly 0 | `hour_sin`, `hour_cos`, `weekday_sin`, `weekday_cos`, `has_comment`, `is_new_page`, `comment_hidden`, `comment_has_link`, `is_temp_account`, `removed_most_of_page`, and both revert counters |
| Maturity window still provisional | 48h placeholder; the cohort ages ~21 August (M2 C-1/C-2) |
| Coverage gap 2026-08-10 to 08-13 | 3,610 minutes, from the outage test; permanent and published |

None of these are blockers for M4. All of them are the sort of thing that
becomes a blocker if it goes into a summary as a rounding error.
