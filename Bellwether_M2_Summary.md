# Bellwether — M2 Summary

**Milestone:** M2 — Maturity, features, and the kill criterion
**Started / completed:** 2026-08-14
**Status:** KC-2 answered. Maturity window outstanding (§8).
**Live:** [/kc2](https://bellwether-fyyz.onrender.com/kc2) ·
[/maturity](https://bellwether-fyyz.onrender.com/maturity)

---

## 1. The verdict

> **Re-measured in M3 and still cleared.** These numbers were produced with
> reverts folded into editor and page state at `revert_ts` — the moment a revert
> happened, rather than the moment this system could have known of it. M3 found
> that and closed it (commit `cd017df`), which changed the state every model
> here was trained on, so the verdict was re-earned rather than assumed:
> **margin +0.1072, CI [+0.0794, +0.1394]**, against the same required +0.0500.
> The table below is left as it was measured. Live figures: `/kc2`.
>
> The re-run also answers a question this summary could not: `editor_edits_reverted`
> and `page_edits_reverted`, the two features the leak actually touched, both
> measure at **exactly 0.000 importance**. The leak was real and worth closing.
> It moved nothing.

**KC-2 is cleared. The project continues past M2.**

| Scorer | PR-AUC |
|---|---|
| **model** | **0.2431** |
| logged-out heuristic | 0.1399 |
| arrival order | 0.0613 |
| absolute byte delta | 0.0570 |

Margin **+0.1032**, 95% CI **[+0.0764, +0.1363]**, required **+0.0500**. On 11,156
matured events with 640 positives and 28 features. The whole interval sits
above the threshold.

Provisional: maturity is fixed at 48 hours because the real window is not yet
estimable (§8). Every number here must be re-run against it.

### 1.1 The verdict rests on one feature, and that feature drifts

Removing `log_user_id` — the most important input — drops the margin to
**+0.0441**, below the +0.0500 required.

**KC-2 still clears, by the rule as written.** The criterion asks whether a
leak-free feature set beats the logged-out heuristic by the margin, and it
does. `log_user_id` is knowable at scoring time, fixed before the edit existed,
and the null check confirms the harness is clean. Adding "and must survive
ablation" after seeing the result would be exactly the post-hoc criterion
change `PREREGISTRATION.md` exists to forbid.

But the honest reading is narrower than the headline:

> There is real signal here, and almost all of it currently arrives through one
> continuous feature whose absolute magnitude increases every month by
> construction.

Account ids only go up. A model leaning on their magnitude will see
systematically different values in September than in August. **The project has
identified the specific mechanism by which its first model will decay, before
that model was ever deployed** — which is, uncomfortably, the system working as
designed.

---

## 2. What was built

| Area | Delivered |
|---|---|
| Knowability guard | `knowability.py` — differential probes, six planted leaks proving it fires, wired into CI before the tests |
| Features | `features.py` — 28 point-in-time features, outcome tags structurally excluded |
| State | `state.py` — one pair of functions for batch replay and M3's online scoring, so there is no second implementation to drift |
| Maturity | `/maturity` publishing cumulative incidence by observed age, with its own reliability flags |
| Evaluation | `evaluate.py` — three baselines, rolling-origin, paired bootstrap, null check, permutation importance, ablation |
| Publication | `/kc2` serving the verdict, its sanity check and its ablation, append-only |

---

## 3. Findings

### 3.1 The guard catches things code review would not

Six planted leaks, each a feature a reasonable person might write. The one worth
naming: **counting raw tags instead of filtered ones**. Nothing in that code
mentions `mw-reverted`. It counts `len(tags)` — and because backfilled rows
carry an extra tag, the count leaks the answer.

Verified against five real production rows whose tag arrays contain
`mw-reverted`: identical feature hashes with and without it.

### 3.2 The spec's own prediction about history features was wrong

§3.3 predicted history features would be useless under a 3% frame — most
registered editors appearing with no prior history.

Measured on framed rows only: **42.2% of logged-out events and 50.0% of
registered ones** have prior editor history. And `editor_edits_seen` and
`editor_days_known` turned out to be the **second and third** most important
features in the model.

The reasoning treated editors as interchangeable. Edit activity is heavily
concentrated, so even a 3% sample catches prolific editors repeatedly — and
prolific editors are most of the events.

### 3.3 `is_logged_out` never appears

The KC-2 opponent is absent from the top eight features, because `log_user_id`
subsumes it: temporary accounts are freshly minted and carry the highest ids,
registered accounts span the full range, and newer ones are riskier than
veterans. One continuous feature encodes the boolean and a finer signal
besides.

| Feature | Importance |
|---|---|
| `log_user_id` | **+0.2076** |
| `editor_edits_seen` | +0.0989 |
| `editor_days_known` | +0.0484 |
| `byte_delta` | +0.0306 |
| `tag_count` | +0.0238 |

### 3.4 The harness is clean

A model fitted to **shuffled labels** scores 0.0563 against a base rate of
0.0572 — **0.98×**, chance. Row order, fold boundaries and everything else in
the evaluation carry no outcome information.

This is the check the knowability guard structurally cannot perform. The guard
proves no feature depends on the future of the event it describes; it says
nothing about the harness consuming them.

---

## 4. The maturity curve was wrong three separate ways

M2's first number took three corrections before it meant anything, and none of
the three raised an error.

**Cumulative incidence fell with age.** 10.62% at 1h, 1.36% at 6h, 0.76% at 24h,
21.27% at 48h. An edit reverted by one hour is still reverted at six, so that
sequence describes nothing that can happen. The denominator counted only events
observed at or beyond each age — and an event that tests positive early is never
re-checked, so it left the denominator at every later age and took its revert
with it. **The query had the bug its own comment claimed to fix.**

**It was binned by the wrong quantity.** `checkpoint_seconds` is the checkpoint
the job was working through; `age_seconds` is when the observation was actually
made. Locally, checks nominally due at six hours were performed at eight. Worse,
a backfilled event reaches four checkpoints at once and records a single
72-hour observation against all of them — including the one labelled "1h".

**It read 100% beyond the observation horizon.** A positive stays known forever
while a negative needs a fresh look, so the denominator collapses to the
positives alone and the rate necessarily tends to 100. Nothing said so; the row
just looked suspiciously round.

Monotonicity is now asserted in the test suite, over a fixture built to
reproduce the exact shape.

---

## 5. Bugs, and one that should not have been possible

| # | Bug | Why it mattered |
|---|---|---|
| 1 | `/stats` read outcomes from an ingestion-frozen tag array | Published 22.04% where the checkpoint data said 38.21%. The property that makes the register trustworthy is what made the number wrong |
| 2–4 | The three maturity-curve errors above | Each produced a plausible curve |
| 5 | **`evaluate.py` had no tests at all** | A syntax error in it passed a green suite of 141. Nothing imported the module that decides whether the project survives |
| 6 | Same syntax error reintroduced | Both times caught only by the test file added after the first |
| 7 | `str` parameter bound to a `jsonb` column | Failed at the very end of a run, after all the work |
| 8 | `evaluate.yml` surfaced no failure detail | The first run reported "exit code 1" and nothing else. Diagnosing a decision procedure by guesswork |

Number 5 is the one to sit with. Every other bug in this project has been caught
by a verification step; that one was the *absence* of a verification step, on
the single most consequential file in the repository. The fix — a test whose
whole content is "the module imports" — is trivial, which is precisely why it
had never been written.

The migration guard added in M1 caught missing `/health` expectations for
migrations 007 and 008, one commit after each was written. That one works.

---

## 6. Production verification

| Claim | Evidence |
|---|---|
| Ten migrations applied | `/health` reports all ten `true`, `schema_behind: []` |
| Guard runs before any training | CI step, and again inside `build_matrix` |
| Guard clean on real contaminated rows | 5 production rows with `mw-reverted` in tags, 0 leaked |
| Verdict is public and append-only | `/kc2`, writer holds no `UPDATE` or `DELETE` |
| Evaluation is manual only | No schedule — a decision procedure on a timer invites re-rolling until a result looks better |

---

## 7. Acceptance criteria

| # | Status |
|---|---|
| C-1 Kaplan–Meier curve per stratum | ⚠️ **Blocked** — see §8 |
| C-2 Maturity window set by the 95% rule | ⚠️ **Blocked** |
| C-3 Guard raises on a planted leak | ✅ Six of them |
| C-4 `mw-reverted` provably absent from features | ✅ Tested, and verified on real rows |
| C-5 `editor_state` coverage measured per stratum | ✅ 42.2% / 50.0% |
| C-6 Model with rolling-origin evaluation | ✅ |
| C-7 PR-AUC of model and all baselines, paired intervals | ✅ |
| C-8 **KC-2 answered in public** | ✅ `/kc2` |

---

## 8. Outstanding

**The maturity window.** A clean estimate needs the maturity cohort — events
ingested live under the frame and observed at every checkpoint as they age.
Backfilled events were observed once, late; they attribute three days of
accumulated reverts to a single observation and cannot separate when a revert
arrived from when we happened to look.

That cohort began accumulating on 13 August. The window needs roughly a week of
it, so the estimate is due around **21 August**. `/maturity` states, in its own
response, that the window is unset and what it is blocked on.

Every figure in §1 must be re-run against it.

---

## 9. Carried into M3

1. **`log_user_id` drift is the top risk.** Replace it with a drift-stable form
   — account age, or a percentile against the maximum id seen so far, both
   point-in-time computable from ingested data. Then re-run the ablation.
2. **State staleness becomes real.** M2 replays state in memory; M3 scores
   online against a persisted table. The gap between them is train/serve skew
   unless the replay runs often enough or the scorer folds in events since.
3. **The append-only prediction register**, and `scored_at >= event_ts` enforced
   by constraint.
4. **The maturity window**, once §8 resolves, applied to every published metric.
