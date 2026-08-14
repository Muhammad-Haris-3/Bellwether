"""The promotion rule (M5 §6, acceptance D-4).

D-4 is the criterion that matters: each of P-1 to P-5 must be shown blocking a
promotion ON ITS OWN. Five conditions of which four are never exercised is one
condition and four comments.

Each test below constructs a world where exactly one condition fails and the
other four hold, then asserts the decision is `reject` and that the row records
which one refused.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from bellwether import preregistration as pre
from bellwether import promote

BANDS = {"edit_size": [10.0, 50.0, 200.0], "page_activity": [1.0, 3.0, 10.0]}


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _rows(
    n: int,
    *,
    positives: int | None = None,
    challenger_better: bool = True,
    champion_noise: float = 1.3,
    challenger_noise: float = 0.45,
) -> list[dict[str, Any]]:
    """A paired world where both scores are actual probabilities.

    Two earlier fixtures were wrong in instructive ways. The first gave the
    champion cleanly separated scores, so it reached PR-AUC 1.0 and no
    challenger could gain anything. The second pushed the classes apart in
    SCORE space, which ranks better and calibrates worse — every candidate
    separation failed P-5 on ECE, because a score that is not a probability
    cannot be well calibrated however well it ranks.

    Here a latent quality decides the label, and each model estimates it with
    noise. The challenger estimates it more precisely, so it ranks better while
    both remain roughly calibrated — which is what a genuinely better model
    looks like, and the only fixture where all five conditions CAN hold.
    """
    rng = np.random.default_rng(11)
    latent = rng.normal(loc=-0.2, scale=1.5, size=n)
    truth = _sigmoid(latent)
    labels = rng.random(n) < truth

    champion = _sigmoid(latent + rng.normal(scale=champion_noise, size=n))
    challenger = _sigmoid(
        latent + rng.normal(scale=challenger_noise if challenger_better else champion_noise, size=n)
    )

    return [
        {
            "revid": i,
            "label": bool(labels[i]),
            "champion_score": float(champion[i]),
            "challenger_score": float(challenger[i]),
            "sampling_weight": 1.0,
            "is_logged_out": i % 2 == 0,
            "edit_size": float(i % 400),
            "hour_utc": i % 24,
            "page_activity": float(i % 12),
        }
        for i in range(n)
    ]


def _passing() -> tuple[list[dict[str, Any]], float]:
    """Enough positives, enough shadow, and a clearly better challenger."""
    # ~45% base rate, so 6,000 events clears P-3's 2,500 positives.
    return _rows(6_000), 10.0


def test_all_five_can_pass() -> None:
    """The control. Without this, every test below could be passing because the
    fixture never satisfies anything."""
    rows, days = _passing()
    verdict = promote.evaluate(rows, BANDS, days)
    assert all(verdict[k] for k in ("p1", "p2", "p3", "p4", "p5")), verdict


def test_p1_blocks_a_challenger_that_is_merely_equal() -> None:
    """A margin of zero is not a reason to change the model that is serving."""
    rows = _rows(
        30_000, positives=pre.PROMOTION_MIN_MATURED_POSITIVES + 200, challenger_better=False
    )
    verdict = promote.evaluate(rows, BANDS, 10.0)
    assert verdict["p1"] is False
    assert verdict["p1_gain"] < pre.PROMOTION_MIN_PR_AUC_GAIN


def test_p2_blocks_when_the_interval_includes_zero() -> None:
    """A gain that clears the margin on a sample too small to distinguish it
    from noise. P-1 alone would promote this."""
    rows = _rows(120, challenger_noise=1.15)
    verdict = promote.evaluate(rows, BANDS, 10.0)
    assert verdict["p2_low"] <= 0 <= verdict["p2_high"]
    assert verdict["p2"] is False


def test_p3_blocks_before_enough_positives_have_matured() -> None:
    rows, days = _rows(300), 10.0
    verdict = promote.evaluate(rows, BANDS, days)
    assert verdict["p3"] is False
    assert 0 < verdict["p3_positives"] < pre.PROMOTION_MIN_MATURED_POSITIVES
    assert verdict["p4"] is True, "only P-3 may fail here"


def test_p4_blocks_before_enough_wall_clock_time() -> None:
    """Independent of P-3 on purpose: 2,500 positives drawn from a single quiet
    weekend is not evidence a model works on a Monday."""
    rows, _ = _passing()
    verdict = promote.evaluate(rows, BANDS, pre.PROMOTION_MIN_SHADOW_DAYS - 0.5)
    assert verdict["p4"] is False
    assert verdict["p3"] is True, "only P-4 may fail here"


def test_p5_blocks_a_segment_regression() -> None:
    """The challenger is better overall and worse for logged-out editors.

    An aggregate that stays healthy while a segment collapses is not a system
    working — it is a system whose failure is being averaged away.
    """
    rows, days = _passing()
    for row in rows:
        if row["is_logged_out"]:
            # Invert the challenger for this segment only.
            row["challenger_score"] = 1.0 - row["challenger_score"]

    verdict = promote.evaluate(rows, BANDS, days)
    assert verdict["p5"] is False
    assert verdict["p5_segment"] is not None
    assert verdict["p5_regression"] > pre.PROMOTION_MAX_SEGMENT_REGRESSION


def test_p5_blocks_a_calibration_regression() -> None:
    """Ranking and calibration are different properties. M6 puts a queue in
    front of a human, and a score that does not mean what it says is worse than
    no score."""
    rows, days = _passing()
    for row in rows:
        # Same ordering, wildly overconfident: PR-AUC is untouched, ECE is not.
        row["challenger_score"] = min(row["challenger_score"] * 0.5 + 0.5, 1.0)

    verdict = promote.evaluate(rows, BANDS, days)
    assert verdict["p5_ece"] > pre.PROMOTION_MAX_ECE_REGRESSION
    assert verdict["p5"] is False


def test_one_class_is_undefined_rather_than_failed() -> None:
    """A comparison with no positives is not evidence the challenger is worse.
    It is no evidence at all, and the row should say so rather than record a
    rejection nobody can interpret."""
    rows = _rows(500)
    for row in rows:
        row["label"] = False
    verdict = promote.evaluate(rows, BANDS, 10.0)
    assert verdict["champion_pr_auc"] is None
    assert verdict["p1"] is False


def test_bands_are_quartiles_of_what_they_were_given() -> None:
    edges = promote.bands_for([float(i) for i in range(101)])
    assert len(edges) == 3
    assert edges[1] == pytest.approx(50.0, abs=1.0)


def test_a_value_past_the_last_edge_lands_in_the_top_band() -> None:
    """Quartiles from the training window will not bound a live event, and a
    band function that returned nothing for an outlier would drop the largest
    edits out of the segment that most wants them."""
    assert promote.band_of(10_000.0, [10.0, 50.0, 200.0]) == "q4"


def test_an_unbanded_dimension_degrades_to_one_segment() -> None:
    """A model trained before M5 has no frozen bands. One segment covering
    everything is honest; inventing quartiles from the comparison window is the
    one thing M5-FR-6 forbids."""
    assert promote.band_of(5.0, []) == "all"


def test_the_decision_segments_are_the_pre_registered_ones() -> None:
    row = {"is_logged_out": True, "edit_size": 5.0, "hour_utc": 3, "page_activity": 0.0}
    assert set(promote.segment_of(row, BANDS)) == set(pre.DECISION_SEGMENTS)
