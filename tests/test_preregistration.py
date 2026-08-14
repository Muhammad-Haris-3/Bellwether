"""The code and the pre-registration must not drift apart (M5-FR-2, D-10).

`PREREGISTRATION.md` was committed before the first model was trained. Its
value comes entirely from being unchangeable after the fact, and a document the
code can quietly diverge from provides none of it — the code would decide, and
the document would describe something that used to be true.

So every constant is asserted against the clause it was taken from, by reading
the document. Editing either alone fails the build.

These tests read the file rather than a parsed copy of it on purpose. A fixture
holding the "expected" text would be a third thing to keep in step, and the
whole point is to have exactly two.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bellwether import preregistration as pre

DOCUMENT = Path(__file__).resolve().parent.parent / "PREREGISTRATION.md"


@pytest.fixture(scope="module")
def text() -> str:
    return DOCUMENT.read_text(encoding="utf-8")


def _clause(text: str, marker: str) -> str:
    """The line naming a condition, e.g. the table row beginning `| P-1 |`."""
    for line in text.splitlines():
        if marker in line:
            return line
    raise AssertionError(f"{marker} is not in PREREGISTRATION.md at all")


def test_the_document_exists_and_is_not_empty(text: str) -> None:
    assert len(text) > 2_000, "a pre-registration this short is not the one this project made"


# --- §5, the promotion rule ------------------------------------------------


def test_p1_margin_matches_the_document(text: str) -> None:
    clause = _clause(text, "| P-1 |")
    assert "0.02" in clause
    assert pre.PROMOTION_MIN_PR_AUC_GAIN == 0.02


def test_p2_is_a_two_sided_interval_excluding_zero(text: str) -> None:
    clause = _clause(text, "| P-2 |")
    assert "excludes zero" in clause
    # 95% interval, so alpha is 0.05. Stated here because P-2 names no number
    # and would otherwise be the one condition nothing checks.
    assert pre.BOOTSTRAP_ALPHA == 0.05
    assert "0.05" in text and "two-sided" in text


def test_p3_is_stated_in_positives_not_events(text: str) -> None:
    """§7's reasoning: the sampling rate was set in M1, so a requirement in
    total events could be moved afterwards by changing it."""
    clause = _clause(text, "| P-3 |")
    assert "2,500" in clause
    assert "positive" in clause.lower()
    assert pre.PROMOTION_MIN_MATURED_POSITIVES == 2_500


def test_p4_is_wall_clock_and_independent_of_p3(text: str) -> None:
    clause = _clause(text, "| P-4 |")
    assert "7" in clause and "day" in clause.lower()
    assert pre.PROMOTION_MIN_SHADOW_DAYS == 7


def test_p5_thresholds_match_the_document(text: str) -> None:
    clause = _clause(text, "| P-5 |")
    assert "0.02" in clause and "0.03" in clause
    assert pre.PROMOTION_MAX_ECE_REGRESSION == 0.02
    assert pre.PROMOTION_MAX_SEGMENT_REGRESSION == 0.03


def test_the_bootstrap_is_the_one_that_was_registered(text: str) -> None:
    assert "2,000 resamples" in text
    assert pre.BOOTSTRAP_RESAMPLES == 2_000


# --- §8, triggers ----------------------------------------------------------


def test_decay_threshold_matches(text: str) -> None:
    clause = _clause(text, "**Decay**")
    assert "0.03" in clause
    assert pre.DECAY_PR_AUC_DROP == 0.03


def test_drift_threshold_matches(text: str) -> None:
    clause = _clause(text, "**Input drift**")
    assert "0.20" in clause
    assert pre.DRIFT_PSI_THRESHOLD == 0.20


def test_a_trigger_needs_three_consecutive_windows(text: str) -> None:
    """One bad day on a rare-positive metric is noise, and a system that
    retrains on noise is not maintaining itself."""
    assert "3 consecutive daily windows" in text
    assert pre.TRIGGER_CONSECUTIVE_WINDOWS == 3


def test_the_floor_interval_matches(text: str) -> None:
    clause = _clause(text, "**Floor**")
    assert "7" in clause
    assert pre.RETRAIN_FLOOR_DAYS == 7


# --- §9, rollback ----------------------------------------------------------


def test_rollback_threshold_and_window_match(text: str) -> None:
    section = text[text.index("## 9. Rollback") :]
    assert "0.02" in section
    assert "14 days" in section
    assert pre.ROLLBACK_PR_AUC_DROP == 0.02
    assert pre.ROLLBACK_WINDOW_DAYS == 14


# --- §4, segments ----------------------------------------------------------


def test_the_decision_segments_are_the_pre_registered_four(text: str) -> None:
    """The conflict M5 §2 records. P-5 refers to THIS list, not to M4's
    diagnostic segments, and using M4's would quietly change what the promotion
    rule tests."""
    section = text[text.index("## 4. Segments") : text.index("## 5. The promotion rule")]
    for phrase in ("editor class", "edit size band", "hour of day", "page activity band"):
        assert phrase in section, phrase
    assert pre.DECISION_SEGMENTS == (
        "editor_class",
        "edit_size_band",
        "hour_of_day",
        "page_activity_band",
    )


def test_the_kill_criterion_is_unchanged(text: str) -> None:
    assert pre.KC2_MARGIN == 0.05


def test_the_rolling_window_is_seven_days(text: str) -> None:
    """Both the decay trigger and the rollback condition are stated over a
    rolling 7-day PR-AUC. One constant, quoted twice in the document."""
    assert "rolling 7-day pr-auc" in text.lower()
    assert pre.ROLLING_WINDOW_DAYS == 7


def test_the_maturity_method_is_fixed_even_though_the_number_is_not(text: str) -> None:
    """§6. The METHOD was fixed before measuring; the window itself is estimated
    in M2 by Kaplan-Meier. Fixing a number before measuring the curve would be a
    guess wearing a commitment's clothes."""
    section = text[text.index("## 6. Maturity") :]
    assert "Kaplan" in section
    assert "95%" in section
    assert pre.MATURITY_SURVIVAL_QUANTILE == 0.95


def test_every_constant_is_covered_by_a_test() -> None:
    """The vacuous-pass guard, applied to this file.

    A constant added to the module and to no test would be exactly the drift
    these tests exist to prevent, and nothing would say so.
    """
    constants = {name for name in dir(pre) if name.isupper() and not name.startswith("_")}
    source = Path(__file__).read_text(encoding="utf-8")
    covered = {name for name in constants if re.search(rf"\bpre\.{name}\b", source)}
    assert constants == covered, f"no assertion for: {sorted(constants - covered)}"
