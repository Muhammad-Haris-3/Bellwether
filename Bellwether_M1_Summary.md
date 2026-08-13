# Bellwether — M1 Summary

**Milestone:** M1 — Durable ingestion within a fixed storage budget
**Started / completed:** 2026-08-13
**Status:** Build complete. One measurement outstanding (§8).
**Live:** [bellwether-phi.vercel.app](https://bellwether-phi.vercel.app) ·
[API](https://bellwether-fyyz.onrender.com/health)

---

## 1. The question M1 existed to answer

> How much of Wikipedia can this project afford to remember, and for how long?

**About 9,300 edits a day, for thirty days of raw material and ninety of
evidence, inside 400 MB.** Everything else in M1 — the sampling rate, the
cohort, retention, sealing, gap healing — follows from that arithmetic.

M1 turned out to be less about building than about *measuring first and finding
that two parts of the specification could not both be true.*

---

## 2. What was built

| Area | Delivered |
|---|---|
| Sampling frame | `bellwether/frame.py` — deterministic, stratified, inverse-probability weights recorded at observation time |
| Schema | Tag dimension, cohort flag, redundant index dropped, evidence decoupled from raw retention |
| Maturity cohort | Full checkpoint grid on a 10% cohort; one check at maturity for the rest |
| Gap healing | `bellwether/gapfill.py` — derives gaps, heals oldest-first, gives up honestly |
| Retention | `landing.prune_expired`, a SECURITY DEFINER function the writer may call but cannot outrank |
| Sealing | `bellwether/seal.py` — monthly SHA-256 digests committed to public git |
| Revert capture | Reverting edits recorded for the whole feed, outside the frame |
| Observability | `/health` reports its own migrations; `/stats` publishes coverage gaps, revert counts, and every rate both raw and weighted |

Six migrations, 118 tests.

---

## 3. Findings that changed the design

### 3.1 The SRS contradicted itself

**Neon Free is 0.5 GB.** NFR-4 caps use at 80%, giving 400 MB. Measured against
real data, `rc_events` costs **372 bytes a row**.

SRS §6.5 specified 120-day raw retention — which permits about 9,000 events a
day. SRS §6.3 specified keeping **100% of logged-out edits** — roughly 14,000 a
day on its own.

**Those two clauses cannot both hold.** They had been in the document since the
morning and only surfaced because M1 began by measuring rather than
implementing. The frame now samples both strata; the alternative — a shorter
evidence window, or no shadow predictions — would have traded something
irrecoverable for something correctable, since a documented probability sample
can always be weighted back and evidence never kept cannot.

### 3.2 The cheaper label path was worth nothing, and was withdrawn

VER-5 measured `recentchanges` re-polling at 500 revisions a request against
`prop=revisions` at 50 — a 50× saving under M0's census.

| | Window re-poll | `prop=revisions` |
|---|---|---|
| M0, 100% ingested | 180 req/day | 9,000 req/day |
| M1 frame, ~10% sampled | **252 req/day** | **262 req/day** |

A window holds the whole population; only a tenth of it is ours. Ten times more
rows per request, to reach a tenth as many rows of interest, cancels exactly.

M1-FR-15 to FR-17 were **withdrawn rather than built**. A second retrieval path,
a horizon fallback and an agreement study, for a 4% saving, would have been work
that looked like progress. The backlog they existed to solve was already gone:
the frame and cohort took 9,000 requests a day down to 262.

### 3.3 What the frame broke instead

**93.8% of reverting edits are made by registered editors**, whom the frame
samples at 3%. The secondary label path derives outcomes from the reverting
edit's own tags at zero API cost — and it read `rc_events`.

Its recall, 19% under a census, would have fallen to roughly **1%**.

Nothing would have errored. A path that had been contributing labels would
simply have stopped, and the published agreement figure between the two label
paths would have become meaningless *while still being published*.

The principle now written into the spec:

> **The frame governs what the project studies. It must not govern what the
> project can observe about outcomes.**

Reverting edits are recorded for the whole feed. Live: 3,000 events seen, 249
sampled, **122 reverting edits captured with zero underivable targets**. Under
the withdrawn design roughly ten of those 122 would have been visible.

---

## 4. What was measured

### Storage, per row

| Table | Bytes/row |
|---|---|
| `rc_events` | **372** (264 heap + 91 index) |
| `label_checks` | ~120 |
| `labels` | ~150 |

Column-level: fixed 120 B, `title` 22.6, `comment` 48.3, `tags` 40.3, `user_name` 10.9.

### Two optimisations measured and rejected

- **A page dimension table.** 5,000 events span 3,927 distinct pages — **1.27
  events per page**. It would have saved nothing.
- **Truncating edit summaries.** The average is already 47 characters.

What did work: **only 67 distinct tags exist**, so a dimension plus `smallint[]`
saves ~50 B, and one composite index was redundant with the plain `event_ts`
index. Together, 372 → ~300 B.

### The frame, verified live

| | |
|---|---|
| Weighted population estimate | **2,889** against 3,000 truly seen — **3.7% error from a 5.3% sample** |
| Cohort share | **10.0%**, exactly |
| Median spacing between kept events | **9 seconds** (p99 38 s, max 51 s) |

That last figure re-validated the ten-minute gap threshold against a frame that
had cut volume tenfold. Sampling thins the stream; it does not make holes.

### Current production state

| | |
|---|---|
| Events stored | 50,765 |
| Revert events (outside the frame) | 232 |
| Labels | 2,299 |
| Storage | **4.5 MB of 400 MB — 1.1%** |
| Coverage | 14.5 h of 79.2 h spanned, 1 known gap |

Matured 48 h+, raw against population-weighted:

| Stratum | n | Raw rate | Weighted n | Weighted rate |
|---|---|---|---|---|
| Logged out | 2,958 | 22.04% | 3,444 | 21.89% |
| Registered | 17,735 | 3.26% | 24,428 | 3.30% |

The two columns are still close because most stored rows predate the frame and
carry a weight of 1.0 — correct, because M0 ingested a census. They will diverge
as framed data accumulates, and both will keep being published.

---

## 5. Six bugs, and one process failure

| # | Bug | Why it mattered |
|---|---|---|
| 1 | **A failing test reached `main`** | The verify command piped pytest into `tail`; a pipeline exits with its *last* command's status, so `tail` succeeded and the `&&` chain pushed. Green on screen, red exit code discarded |
| 2 | A `%` inside a SQL **comment** | psycopg reads it as a placeholder start. The error names neither the comment nor the line |
| 3 | `tag_names`, then `gap_attempts`, missing from the test truncate list | Order-dependent failures that accuse the wrong code. Fixed by *discovering* tables from `information_schema` rather than listing them |
| 4 | Migration 005 absent from `/health` expectations | `schema_behind` was empty and status `ok` while blind to it. A check that does not exist cannot fail |
| 5 | Migration 006, same omission | Caught by the guard written for #4, one commit later |
| 6 | `inputs.apply` is `""` on a scheduled run | `[ "$x" = "false" ]` would have been false, and retention would have **deleted by accident on exactly the trigger that matters** |

Bug 1 is the one worth dwelling on. Every other bug in this project has been
caught by a verification step; that one was a failure *of* the verification
step, and it let a red suite through. Fixed with `set -o pipefail`.

Four of six are the same shape as M0's: **something reports success while having
checked nothing.** M1 found it inside the mechanism built to detect exactly that
drift. That is now the documented failure mode of this codebase.

---

## 6. Production verification

| Claim | Evidence |
|---|---|
| Six migrations applied | `/health` reports all six `true`, `schema_behind: []` |
| Frame live | First production run kept 8.1% of 4,000 events; 71.7% of kept rows logged out, against 75.6% predicted |
| Revert capture live | 232 revert events recorded, across all three methods |
| Gap healer live | Ran unprompted, recovered ~96 minutes of the Aug-10 discontinuity |
| Writer still cannot delete | Verified on Neon: eight forbidden operations refused |
| Retention reports against budget | 4.5 MB of 400 MB, nothing due |

### B-4, verified in detail

149 rows deleted from 04:00–04:30Z, **behind** a cursor at 05:27:54Z.

| Step | Result |
|---|---|
| Ordinary ingest run | +140 events, cursor → 05:58:10Z, **gap unchanged** |
| Gap-fill job | **149 rows recovered** — exactly the number deleted |
| Cursor after healing | **unmoved** |

Recovery was exact rather than approximate because the frame is deterministic:
re-reading the window re-selected the identical sample. That is the
reproducibility property `frame.py` claims, demonstrated end to end.

---

## 7. Acceptance criteria

| # | Status |
|---|---|
| B-1 frame applied with stratum and weight at observation time | ✅ |
| B-2 raw and weighted rates both published and differing as predicted | ✅ |
| B-3 projected 90-day storage under 400 MB | ✅ 1.1% currently |
| B-4 deliberate gap healed by the gap-fill job, not an ordinary run | ✅ §6 |
| B-5 retention deletes only what the dry run predicted | ✅ asserted in tests |
| B-6 a month sealed and verifiable from git alone | ✅ mechanism tested; first real seal falls due 1 September |
| B-7 reverting edits recorded outside the frame | ✅ 232 live |
| B-8 `label_checks` stops falling behind | ⚠️ **expected, not yet demonstrated** — see §8 |
| B-9 storage reported by the API and visible | ✅ |

---

## 8. Outstanding

**The 24-hour population re-measurement.** The frame is sized on a 15.7%
logged-out share drawn from a daytime window; a 03:00 UTC window measured 5.2%.
The share is strongly diurnal, so the population inputs are probably
over-estimates and the frame conservative on storage.

The frame is deliberately **not** being adjusted. Changing it after ingestion
has run under it does not refine downstream estimates — it invalidates
comparisons across the change. What is owed is a full diurnal cycle reported
*against* the assumption, not used to quietly revise it.

**B-8** needs the same elapsed time: the frame went live at 19:12Z and a claim
that the labeller keeps up needs more than twenty minutes of evidence.

---

## 9. Carried into M2

1. **The maturity window**, estimated by Kaplan–Meier from the cohort grid.
2. **The per-stratum hypothesis from M0**: reverts of registered editors' edits
   appear to arrive about twice as slowly. `PREREGISTRATION.md` §6 already
   permits separate windows if the curves differ.
3. **A knowability guard** before any feature is computed, per SRS FR-14.
4. **KC-2**: if no leak-free feature set beats the logged-out heuristic — which
   M1's own numbers put at 22% against 3.3% — the project stops at M2.
