"""The KC-2 decision procedure.

This module had no test at all when it first ran, and a syntax error in it
passed a green suite: nothing imported it. A decision procedure that can end
the project is a strange thing to leave uncovered.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from bellwether import evaluate


def test_the_module_imports() -> None:
    """The test that would have caught the syntax error.

    Trivial, and it is here because 141 passing tests did not notice that this
    file could not be parsed.
    """
    assert evaluate.KC2_MARGIN == 0.05


def test_the_kc2_margin_matches_the_spec() -> None:
    """Fixed in Bellwether_M2_Spec.md §4 before any model existed.

    Asserted here so lowering it becomes a deliberate act with a failing test
    attached, rather than an edit nobody reviews after a disappointing result.
    """
    spec = (evaluate.__file__.rsplit("bellwether", 1)[0]) + "Bellwether_M2_Spec.md"
    with open(spec, encoding="utf-8") as fh:
        text = fh.read()
    assert "+0.05 PR-AUC absolute" in text
    assert evaluate.KC2_MARGIN == 0.05


def test_parse_when_returns_an_aware_datetime() -> None:
    """The workflow passes strings. A str parameter reaching a timestamptz
    column is sent as text and Postgres has no implicit assignment cast — a
    failure that surfaces at the very end of a run, after all the work."""
    when = evaluate.parse_when("2026-08-10T00:00:00Z")
    assert when == datetime(2026, 8, 10, tzinfo=UTC)
    assert when.tzinfo is not None


def test_baselines_produce_one_score_per_event() -> None:
    rows = [
        {"is_anon": False, "is_temp": True, "oldlen": 100, "newlen": 10},
        {"is_anon": False, "is_temp": False, "oldlen": 100, "newlen": 105},
    ]
    scores = evaluate.baseline_scores(rows)
    assert set(scores) == {"arrival_order", "logged_out", "abs_byte_delta"}
    for name, values in scores.items():
        assert len(values) == len(rows), name

    # The opponent that matters ranks the logged-out edit above the other.
    assert scores["logged_out"][0] > scores["logged_out"][1]
    assert scores["abs_byte_delta"][0] > scores["abs_byte_delta"][1]


def test_arrival_order_is_the_absence_of_a_model() -> None:
    """Descending, because a patroller works through newest first and the
    baseline has to be the status quo rather than a lucky ordering."""
    rows = [{"is_anon": False, "is_temp": False, "oldlen": 0, "newlen": 0}] * 3
    order = evaluate.baseline_scores(rows)["arrival_order"]
    assert list(order) == sorted(order, reverse=True)


def test_the_paired_bootstrap_finds_no_difference_between_identical_scorers() -> None:
    """A test whose failure would mean every margin this project publishes is
    inflated by construction."""
    rng = np.random.default_rng(0)
    y = (rng.random(400) < 0.2).astype(int)
    scores = rng.random(400)

    observed, lo, hi = evaluate.paired_bootstrap(y, scores, scores, resamples=200)
    assert observed == pytest.approx(0.0, abs=1e-12)
    assert lo <= 0 <= hi


def test_the_paired_bootstrap_detects_a_real_difference() -> None:
    rng = np.random.default_rng(1)
    y = (rng.random(800) < 0.25).astype(int)
    informative = y + rng.normal(0, 0.35, 800)
    noise = rng.random(800)

    observed, lo, _ = evaluate.paired_bootstrap(y, informative, noise, resamples=200)
    assert observed > 0.2
    assert lo > 0


def test_rolling_origin_never_scores_the_training_rows() -> None:
    """M2-FR-15. A random split would let the model see the future of the very
    editors and pages it is scoring — the leak the knowability guard exists to
    prevent, reintroduced by the evaluation instead of the features."""
    rng = np.random.default_rng(2)
    n = 500
    matrix = rng.random((n, 3))
    labels = (rng.random(n) < 0.3).astype(int)

    scores, scored = evaluate.rolling_origin_scores(matrix, labels, folds=4)

    # The first fold is training data only and must never be scored.
    first_fold_end = int(n * 1 / 5)
    assert not scored[:first_fold_end].any()
    assert len(scores) == int(scored.sum())
