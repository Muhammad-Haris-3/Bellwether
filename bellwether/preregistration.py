"""The constants `PREREGISTRATION.md` fixed before any model existed (M5 §1).

Every number here is a **quotation**. This module does not decide anything; it
mirrors a document that was committed before the first model was trained, so
that the code and the commitment cannot drift apart without something failing.

`tests/test_preregistration.py` reads the document and asserts each value
against the clause it comes from. Editing one without the other breaks the
build, which is the only thing that makes a pre-registration different from a
document (M5-FR-2).

**If a threshold is wrong, that is an amendment with a date and a reason, made
before the run it would change.** Never after seeing which way a decision fell.
The whole apparatus — the append-only register, the digest-verified artifacts,
the decision log — exists to make that distinction checkable by someone who was
not here.
"""

from __future__ import annotations

from typing import Final

# --- §5, the promotion rule ------------------------------------------------
#
# A challenger replaces the champion only if ALL FIVE hold. Failing any one, it
# is rejected and the champion stays — recorded with the same evidence a
# promotion would carry.

# P-1: PR-AUC must exceed the champion's by at least this, absolute, on the
# same matured events.
PROMOTION_MIN_PR_AUC_GAIN: Final = 0.02

# P-2 is not a number: the paired bootstrap 95% interval for that difference
# must exclude zero. Encoded by BOOTSTRAP_ALPHA below.

# P-3: matured positives accumulated in shadow. Stated in POSITIVES rather than
# events on purpose (§7) — the sampling rate was set in M1, so a requirement in
# total events could be moved afterwards by changing it. A requirement in
# positives cannot.
PROMOTION_MIN_MATURED_POSITIVES: Final = 2_500

# P-4: wall-clock days of shadow. Independent of P-3 because sample size and
# calendar time are not interchangeable — 2,500 positives drawn from a single
# quiet weekend is not evidence a model works on a Monday.
PROMOTION_MIN_SHADOW_DAYS: Final = 7

# P-5: calibration must not degrade by more than this, and no pre-registered
# segment may regress in PR-AUC by more than the second figure.
PROMOTION_MAX_ECE_REGRESSION: Final = 0.02
PROMOTION_MAX_SEGMENT_REGRESSION: Final = 0.03

# --- §5, the test ----------------------------------------------------------
#
# Paired bootstrap over EVENTS. Resampling the models independently would break
# the pairing and inflate the standard error, so a genuinely better challenger
# would fail to clear the margin and the failure would look like the
# challenger's fault rather than the test's.
BOOTSTRAP_RESAMPLES: Final = 2_000
BOOTSTRAP_ALPHA: Final = 0.05

# --- §8, what triggers a retrain -------------------------------------------
#
# Any one of these. Three CONSECUTIVE windows rather than one: a single bad day
# on a rare-positive metric is noise, and a system that retrains on noise is not
# maintaining itself, it is twitching.

# Decay: rolling 7-day PR-AUC below the champion's registered baseline by more
# than this.
DECAY_PR_AUC_DROP: Final = 0.03

# Input drift: population stability index above this, on any monitored feature
# or on the score distribution.
DRIFT_PSI_THRESHOLD: Final = 0.20

# How many consecutive daily windows a condition must hold before it fires.
TRIGGER_CONSECUTIVE_WINDOWS: Final = 3

# Floor: days since the last training run, regardless of the other two.
RETRAIN_FLOOR_DAYS: Final = 7

# The rolling window every trigger metric is computed over.
ROLLING_WINDOW_DAYS: Final = 7

# --- §9, rollback ----------------------------------------------------------
#
# A system that can promote but never retreat has only half a mechanism.

# If a newly promoted champion's rolling 7-day PR-AUC falls below the PREVIOUS
# champion's registered level by more than this...
ROLLBACK_PR_AUC_DROP: Final = 0.02

# ...within this many days of the promotion decision, the previous champion is
# automatically restored.
ROLLBACK_WINDOW_DAYS: Final = 14

# --- §4, the segments decisions are made over ------------------------------
#
# NOT M4's diagnostic segments. P-5 refers to this list, and M4 chose a
# different four for diagnosis before the conflict was noticed (M5 §2). Both
# are published; only these bind a decision.
#
# Quartile boundaries for the banded segments are taken from the TRAINING
# window and frozen with the model version (M5-FR-6). Recomputing them per
# evaluation window would let a segment regress because the bands moved rather
# than because the model did.
DECISION_SEGMENTS: Final = (
    "editor_class",
    "edit_size_band",
    "hour_of_day",
    "page_activity_band",
)

# --- §6, maturity ----------------------------------------------------------
#
# The METHOD is fixed here; the number is measured in M2. Fixing a number before
# measuring the curve would be a guess wearing a commitment's clothes.
MATURITY_SURVIVAL_QUANTILE: Final = 0.95

# --- §2, the kill criterion ------------------------------------------------
#
# Unchanged since M2 and the only kill criterion on model quality.
KC2_MARGIN: Final = 0.05
