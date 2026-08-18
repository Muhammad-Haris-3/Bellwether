# Bellwether — M9 Summary: Operational Integrity

**Date:** 2026-08-17
**Status:** Transfer accounting and feature-set decoupling shipped. State drift
diagnosed and its largest cause fixed; two smaller causes recorded and open.

There is no M9 spec. This milestone was not planned — it is what a review of a
healthy-looking system turned up, and it is written down because two of the
three findings mean published numbers were resting on something that was not
true.

---

## 1. The result

The system was green. Every scheduled workflow succeeded, the API answered in
under a second, and the register held 81,188 predictions. Underneath:

| Finding | Severity | State |
|---|---|---|
| No counter on the database's transfer allowance | Would have stopped the project | Fixed |
| The feature set could not change without breaking production | Blocked the one open modelling finding | Fixed |
| Persisted state agrees with a replay on **62.47%** of keys | Model served wrong features | Largest cause fixed; two open |

The third is the one that matters. `editor_edits_seen`, `page_edits_seen` and
four other history features have been wrong in production, which means scores
in the append-only register were computed from inputs that cannot be
reconstructed. The register is not wrong about what it predicted — it is wrong
about being reproducible, which is the claim M3 exists to support.

---

## 2. Transfer accounting (NFR-13, R-7a)

NFR-4 capped storage and `retention` has reported against it since M3. Nothing
capped **data transfer**, which is a separate Neon Free allowance with a worse
failure mode: storage exhaustion refuses writes, transfer exhaustion refuses
**connections**, so every job and the API stop together.

This is not hypothetical. GridCast — same author, same plan, same architecture
— exhausted its transfer allowance on 2026-08-17 and stopped completely. Its
append-only register stopped growing and could not even be exported, because
exporting is reading. Nothing was counting, so the first signal was a refused
connection, roughly two weeks before the allowance resets.

Bellwether runs ~227 scheduled jobs a day against the same ceiling.

**Shipped.** A metered cursor in `db.py` counts every row handed back;
`landing.db_transfer` records it per job; the watchdog raises it as an
edge-triggered fault at 80%; `maintain` reports the itemised figure daily. Past
90% the deferrable jobs stand down — ingestion and scoring never do, because an
event outside the recentchanges window is gone and a prediction unscored is
evidence permanently missing.

The byte figure is an **estimate**, measured from returned value widths. It
cannot see framing, TLS or compression and will disagree with Neon's own
number. It says so in the module, the column comment, the payload and the
alert, and it is deliberately not tuned against the console figure to look more
authoritative. The trend is the product; the total is not.

---

## 3. The feature set could not change (PREREGISTRATION §11)

§11 says the feature set is free to change while the rule that judges it stays
fixed. It was not free. Three couplings made adding one feature either break
production or invalidate the register:

1. **Every model was scored against `features.feature_names()`** rather than
   its registered list. That list is *sorted*, so a new feature does not
   append — it inserts. `byte_delta_ratio` would land after `byte_delta` and
   shift every column after it. The champion would not merely get an extra
   input; it would get most of its inputs in the wrong places and carry on
   producing confident, meaningless scores. Its `predict_proba` is unguarded,
   so in practice the first run after any feature change took scoring down.

2. **The challenger was handed the champion's row.** A challenger carrying one
   new feature raises on every event, is swallowed by the per-event "no
   opinion" handler, and accumulates zero paired observations — so P-3 (2,500
   matured positives) could never be reached and the promotion rule could never
   fire. It reads as a broken model rather than an incompatible one.

3. **`feature_hash` digested the whole vector**, including inputs the model
   never saw. Adding a feature would change the recomputed hash of every
   historical prediction, so `reproduce` would report the register as
   unreproducible and the watchdog would raise the reproducibility fault — the
   one that produced a hundred consecutive red runs the last time it fired,
   over a change that broke nothing.

**Shipped.** `feature_hash` takes the subset to digest; each model is scored and
re-derived against its own registered list, in its registered order — the order
it was trained in. A prediction's hash now depends on that prediction and on
nothing that happened afterwards.

The vector is still built **once** per event and selected from twice. The
comment defending that invariant is right — two builds are two chances to
disagree, and the paired comparison assumes they cannot. Two selections from
one vector cannot.

Backward compatibility was asserted, not assumed: hashes already in the
register were computed over the full vector by a model whose registered list
was the full set then in force, so passing that list reproduces the old digest
exactly. Verified against 882 real production predictions — reproduction moved
91.65% → 92.63%, within sampling noise.

---

## 4. State drift: 62.47% agreement

### 4.1 How it was found

`reproduce` had been failing daily since 2026-08-14 with no cause attached, and
its own failure message said why:

> Only the feature hash is stored, not the vector, so this says something
> differs without saying what.

There was nothing to look at. Two hypotheses were formed and **both were
disproved**: `editor["first"]` is seeded from the persisted row and never
lowered below it, so the `LEAST` asymmetry cannot reach a feature; and
`max_user_id` is a running max folded in the same order on both sides. A third
guess would have been worth less than a measurement.

So `--explain` was built. A hash cannot be inverted, so for each failure it
searches instead: the recomputed vector is amended one feature at a time to a
small set of principled candidates and the digest retried. A candidate that
reconciles the hash names both the feature that diverged and what the scorer
actually saw. Only single substitutions are tried — pairs grow as the square
and a coincidental collision stops being unlikely — and finding nothing is
reported as the real answer it is.

Against production it was unambiguous: every named failure was
`editor_edits_reverted` or `page_edits_reverted`, the two counters
`observe_revert` moves together. That pointed at `reconcile`, which had been
measuring the whole problem all along:

```
StateDivergence: 23,616 divergences across 62,929 in-window keys
                 (agreement 62.4720%)

  page.edits                8,993     page.reverted             1,873
  editor.edits              4,578     editor.edits_reverted     1,716
  editor.first              3,333     editor.reverts_performed    510
  page.first                2,612     frontier.value                1

  editor 'Up2show'.edits:        replay says 5,  stored says 10
  editor '~2026-41726-02'.edits: replay says 4,  stored says 3
  editor 'Escape Orbit'.first:   replay 2026-08-11, stored 2026-08-13
```

The 7.4% reproduction failure was a **symptom**. It is the fraction of sampled
predictions whose particular editor and page happened to be diverged at that
moment. The disease is that persisted and replayed state agree on 62.5% of
keys.

### 4.2 The cause

**State is folded as a side effect of scoring, and the scorer's event set is
model-dependent while a replay's is the raw feed.** They cannot agree by
construction. That produces drift in both directions:

**Stored above replay — the doubling.** `UNSCORED_SQL` gates on
`p.model_version = %(model_version)s`. The registry reports two model versions.
When the champion changed, every event inside the lookback window that the old
champion had scored became eligible again for the new one, was re-scored, and
`state.observe` folded it a **second** time. `persist` then wrote
`edits_seen = EXCLUDED.edits_seen` — an absolute count seeded from the
already-inflated row. One promotion, one extra fold, exactly double.

The prediction insert is idempotent through `ON CONFLICT`, which is why this
went unnoticed for four days: re-scoring writes no duplicate *evidence*, only
duplicate *state*.

**Stored below replay.** The same query carries
`AND NOT (e.event_ts >= training_start AND e.event_ts < training_end)` — the
scorer refuses to score inside the champion's training window, so it never
folds those events. A replay folds every raw event in its window regardless.

### 4.3 What was fixed

`landing.state_applied_events`, a sibling of `landing.state_applied_reverts`.
That pattern has existed since M3 and does exactly this for reverts: a revert is
folded once, ever, and the query that finds pending ones excludes what it
already holds. **Reverts got idempotent folding; ordinary events never did.**

The scorer now folds an event only if the ledger does not already hold it, and
records what it folded in the same transaction as the persist it describes.
Scoring is unaffected — a re-scored event still gets its prediction, only the
counter update is skipped. The two manual rebuild paths (`state.run` and
`reconcile --repair`) record their replayed revids for the same reason, so a
rebuild cannot reintroduce the doubling through a different door.

### 4.4 What is NOT fixed

Recorded rather than papered over:

1. **A re-scored event reads a history containing its own first fold.** The
   state it loads was persisted with that event already in it. Skipping the
   second fold stops the inflation; it does not make the re-scored prediction's
   history point-in-time correct. This is a leak — small, bounded to events
   re-scored across a champion change, and real.
2. **The training-window gap.** Inherent to folding-during-scoring; the online
   path can never fold what it refuses to score.

Both belong to **decoupling state from scoring** — folding every ingested event
exactly once, driven by ingestion rather than by which model needs a
prediction. That is the correct architecture and it is a larger change than
this one.

3. **The existing drift is not repaired.** `reconcile --repair` was not run, for
   the reason `reconcile` itself gives: *"a job that quietly corrects drift
   removes the only signal that something is producing it."* Repair should
   follow confirmation that the fix holds, not precede it.

---

## 5. Consequences for what has been published

Stated plainly because the alternative is letting it stand:

- Predictions written after the champion change, for editors and pages active
  in that lookback window, used inflated `editor_edits_seen` and
  `page_edits_seen`. Those scores are what they are; the register records them
  honestly. What is not true is that they can all be re-derived.
- The reproduction rate published while this was live understates
  reproducibility for a reason unrelated to the scorer's correctness, and
  overstates the health of the state layer.
- **No accuracy claim is invalidated by this** — the maturity window, the
  outcome-blinding and the leakage guards are untouched. The affected quantity
  is the feature input, not the label or the metric.

---

## 6. Carried forward

1. **Decouple state folding from scoring.** Fold every ingested event exactly
   once at ingestion. Removes both remaining causes at their root.
2. **Repair the accumulated drift**, once the fix is confirmed holding —
   `reconcile --repair`, deliberately, with the before and after recorded.
3. **The single-feature dependency is still open.** `account_newness` carries
   the margin (0.107 with, 0.039 without) and twelve of twenty-eight features
   measure zero importance. §3 removed the blocker; the work itself has not
   been attempted, and it should not be attempted while the state layer six of
   those features read from is at 62% agreement.
4. **`PREDATES_SQL` cannot fire yet.** It looks for predictions older than 30
   days and the register is seven days old, so `state predates the window`
   reports 0 and excludes nothing. Correct today, misleading the moment the
   project is older than its own history window.
