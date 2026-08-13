# Bellwether — M3 Specification

**Milestone:** M3 — Scoring, and the register that makes it checkable
**Date:** 2026-08-14
**Status:** Not started
**Depends on:** `Bellwether_SRS_v1.0.md`, `PREREGISTRATION.md`, `Bellwether_M2_Summary.md`

---

## 1. What M3 is for

M2 established that signal exists. M3 makes the system **commit to a score
before the outcome exists**, in a way nobody — including its author — can
revise afterwards.

That is the whole evidential core of the project. Everything M0 built to
guarantee the *outcome* was honestly observed is worth nothing if the
*prediction* can be edited once the outcome is known.

Three things have to be true when M3 ends:

1. Every sampled edit gets a score, written before its outcome is knowable.
2. No role in the system can alter or delete a score once written.
3. Any historical score can be recomputed from what was stored, and matched.

---

## 2. The risk M2 handed over

**`log_user_id` drifts by construction, and KC-2 rests on it.**

Removing it drops the margin from +0.1032 to +0.0441, below the +0.05
threshold. Account ids only increase, so a model leaning on their absolute
magnitude sees systematically different values every month.

M2 deliberately did not fix this: §6 of that spec forbade model tuning, and
KC-2 asked whether signal exists rather than how much could be extracted.

**M3 must fix it before deploying.** Shipping a model whose dominant feature is
known to drift, when a drift-stable alternative exists and costs nothing, would
be choosing to deploy a predictable failure in order to keep a number.

| # | Requirement |
|---|---|
| M3-FR-1 | `log_user_id` shall be replaced by a drift-stable form: account age where derivable, or the id's percentile against the maximum seen in ingested data before this event |
| M3-FR-2 | The replacement shall be point-in-time computable — the running maximum is folded in like any other state, never read from the whole table |
| M3-FR-3 | The evaluation shall be re-run and **both** the margin and the ablation reported. If the drift-stable form drops the margin below +0.05, that is reported, not reverted |

M3-FR-3 is the uncomfortable one. The honest outcome may be that the
drift-stable feature is weaker and KC-2 no longer clears. That result gets
published; it does not get fixed by putting the drifting feature back.

### 2.1 Result, measured 2026-08-14

**The prediction above was wrong, in the good direction.**

| | With `log_user_id` | With `account_newness` |
|---|---|---|
| Model PR-AUC | 0.2431 | **0.2564** |
| Margin | +0.1032 | **+0.1165** |
| 95% CI | [+0.0764, +0.1363] | [+0.0882, +0.1506] |
| Null check | 0.98× base | 0.99× base |
| **Ablation margin** | +0.0441 | **+0.0441** |

The drift-stable form is *better*, not weaker. Expressing an id against the
frontier evidently carries the signal that mattered — "how new is this account
relative to what exists" — while the absolute magnitude was carrying that plus
noise that would have expired.

`is_logged_out` also reappears in the top eight (+0.0184), where `log_user_id`
had subsumed it entirely. A ratio does not encode "temporary accounts sit at
the frontier" as completely as a raw magnitude did, so the boolean now earns
its place again.

**What has not changed is the concentration.** The ablation margin is +0.0441,
identical to before, because removing the top account feature leaves the same
twenty-seven others either way. The signal is still carried mostly by one
feature.

That is a different and smaller problem than it was. Concentration in a feature
that *decays* is a scheduled failure; concentration in one that *does not* is a
robustness concern — the model would suffer if that signal ever became
unavailable, which is worth knowing and is not the same as knowing it will
degrade next month.

---

## 3. The prediction register

| # | Requirement |
|---|---|
| M3-FR-4 | Every sampled event shall be scored by the champion and written to `register.predictions` with `scored_at`, `model_version`, `feature_hash` and `role = 'champion'` |
| M3-FR-5 | The register shall be append-only **by grant**. The pipeline role holds `INSERT` and no `UPDATE`, `DELETE` or `TRUNCATE` |
| M3-FR-6 | `CHECK (scored_at >= event_ts)` shall make a backdated score structurally impossible |
| M3-FR-7 | The register shall carry `role` from the outset, so M5's shadow scoring needs no migration on the evidential table |
| M3-FR-8 | A score shall be written **once** per (revid, model_version, role). Re-running the scorer shall be idempotent by constraint |

### 3.1 Scoring latency is a published figure, not an aspiration

SRS §3.2 records that this is a near-real-time system, not a streaming one. The
lag between an edit and its score is one polling interval plus whatever
GitHub's scheduler adds — measured at roughly hourly in M1, against a nominal
ten minutes.

| # | Requirement |
|---|---|
| M3-FR-9 | `scored_at - event_ts` shall be published as a distribution, not a target |
| M3-FR-10 | A score written after its own outcome was already observable shall be flagged in the register and **excluded from every accuracy claim** |

M3-FR-10 matters more than it looks. If the scorer falls far enough behind, it
could score an edit that has already been reverted — and the revert would be
visible in `revert_events` at that moment. Nothing would raise. The score would
simply be trivially correct, and the accuracy figures would improve for the
worst possible reason.

---

## 4. Train/serve skew

M2 replayed state in memory. M3 scores online against a persisted table, and
that is where the two implementations could diverge.

**They do not, because there is only one.** `state.observe` and
`state.history_for` are already shared. What M3 adds is persistence with the
same ordering: read state, score, fold in, write.

| # | Requirement |
|---|---|
| M3-FR-11 | Scoring shall read persisted state, score in `event_ts` order, and fold each event in **after** its score is emitted |
| M3-FR-12 | The replay in `state.py` shall become a repair operation, and a scheduled job shall assert that replaying reproduces the persisted state |
| M3-FR-13 | Any divergence between replayed and persisted state shall fail loudly, not be silently corrected |

M3-FR-13 is deliberate. A repair job that quietly fixes drift removes the only
signal that something is producing it.

---

## 5. The model registry

| # | Requirement |
|---|---|
| M3-FR-14 | The champion artifact shall be committed to the repository with its metric card, so a promotion is verifiable from git alone by someone with no database access |
| M3-FR-15 | Each version shall record training window, feature list, hyperparameters, offline metrics and the artifact's SHA-256 |
| M3-FR-16 | The scorer shall verify the artifact hash before loading, and refuse to score if it does not match the registry |

**Known fragility, recorded rather than solved.** A pickled scikit-learn model
is tied to its library version. The version is pinned and the hash is checked,
so a mismatch fails loudly instead of scoring differently — but a portable
format would be better, and this is a deliberate deferral rather than an
oversight.

---

## 6. Reproducibility

| # | Requirement |
|---|---|
| M3-FR-17 | A stored prediction shall be recomputable: same event, same state, same model version, same `feature_hash`, same score |
| M3-FR-18 | A scheduled job shall re-derive a sample of historical predictions and assert they match, publishing the agreement rate |

FR-18 is the one that makes FR-17 mean something. A reproducibility claim
nobody re-checks is a comment.

---

## 7. Storage

At the M1 frame, ~9,300 events/day at one prediction each:

| | |
|---|---|
| Row cost | ~120 B |
| Per day | ~1.1 MB |
| At 90-day retention | **~100 MB** of the 400 MB budget |

M5's shadow doubles it, which M1 §2.2 already budgeted for. Current usage is
about 5 MB, so there is room — but predictions become the largest table in the
project, and the retention and sealing built in M1 apply to them from the first
row.

| # | Requirement |
|---|---|
| M3-FR-19 | Predictions shall be sealed monthly and pruned at 90 days, using the M1 mechanism unchanged |
| M3-FR-20 | The pruning function shall refuse to delete predictions from an unsealed month, as it already does for labels |

---

## 8. Acceptance criteria

| # | Criterion |
|---|---|
| D-1 | Every sampled event in a window has exactly one champion score |
| D-2 | The writer is **proven unable** to update or delete a prediction, on the production database |
| D-3 | A backdated score is rejected by the database, demonstrated |
| D-4 | Scoring lag published as a distribution |
| D-5 | Any score written after its outcome was observable is flagged and excluded |
| D-6 | Persisted state matches a full replay, asserted by a job that fails on divergence |
| D-7 | The champion artifact is in git with its hash, and the scorer refuses a mismatch |
| D-8 | A sample of historical predictions recomputes exactly |
| D-9 | The drift-stable feature replaces `log_user_id`, with the new margin **and** ablation published either way |

---

## 9. What M3 must not become

- **Improving the model.** The champion is M2's model with one feature
  replaced for a stated reason. Anything else is M5's business, decided by the
  pre-registered rule rather than by preference.
- **Hiding a worse number.** If the drift-stable feature costs more margin than
  expected, that is the finding.
- **Treating the register as ordinary storage.** It is the only artefact in the
  project that cannot be rebuilt. Everything else — features, state, even the
  raw events — can be recomputed or re-fetched. A score, once lost or altered,
  is gone.
