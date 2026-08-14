"""What causes a retrain (M5-FR-11 to FR-14).

The three conditions are quoted from the pre-registration and asserted against
it elsewhere. These tests are about the parts the document leaves to the
implementation, which is where a trigger goes wrong: what counts as
consecutive, what a reference distribution is, and whether an evaluation that
fires nothing leaves a trace.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pytest

from bellwether import preregistration as pre
from bellwether import triggers
from bellwether.db import connect

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


def test_psi_is_zero_for_an_identical_distribution() -> None:
    rng = np.random.default_rng(0)
    sample = rng.normal(size=5_000)
    assert triggers.psi(sample, sample.copy()) == pytest.approx(0.0, abs=1e-9)


def test_psi_rises_when_the_distribution_moves() -> None:
    rng = np.random.default_rng(0)
    reference = rng.normal(size=5_000)
    shifted = rng.normal(loc=1.5, size=5_000)
    value = triggers.psi(reference, shifted)
    assert value is not None and value > pre.DRIFT_PSI_THRESHOLD


def test_psi_declines_to_answer_on_a_tiny_sample() -> None:
    """A number computed from twelve events looks exactly like a number
    computed from twelve thousand, and only one of them should fire a retrain."""
    rng = np.random.default_rng(0)
    assert triggers.psi(rng.normal(size=12), rng.normal(size=12)) is None


def test_psi_declines_to_answer_on_a_constant_feature() -> None:
    """It cannot drift in any way this measures. Returning 0.0 would be
    indistinguishable from having checked it."""
    flat = np.ones(1_000)
    assert triggers.psi(flat, flat) is None


def test_an_unseen_value_alone_does_not_fire_a_retrain() -> None:
    """Without smoothing, one empty bin sends a term to infinity and a single
    novel value trips the threshold on its own."""
    rng = np.random.default_rng(0)
    reference = rng.normal(size=5_000)
    current = np.concatenate([rng.normal(size=4_999), [50.0]])
    value = triggers.psi(reference, current)
    assert value is not None and value < pre.DRIFT_PSI_THRESHOLD


def test_the_training_window_is_a_function_of_the_trigger_date() -> None:
    """M5-FR-16. A window chosen per retrain is a hyperparameter picked after
    seeing the data, wearing a schedule's clothes."""
    start, end = triggers.training_window(date(2026, 9, 1))
    assert (end - start).days == triggers.TRAIN_WINDOW_DAYS
    # Ends a maturity horizon back: training on events whose outcome is not yet
    # known would fill the positive class from whatever was labelled early.
    assert end.date() == date(2026, 9, 1) - timedelta(days=triggers.TRAIN_WINDOW_LAG_DAYS)


# --- against the database ---------------------------------------------------


def _champion(conn: Any, version: str = "champ", *, trained_days_ago: int = 2) -> None:
    conn.execute(
        """
        INSERT INTO register.model_registry
            (model_version, trained_at, training_start, training_end, n_train_events,
             n_train_positives, feature_names, hyperparameters, offline_metrics,
             artifact_path, artifact_sha256)
        VALUES (%s, now() - make_interval(days => %s), now() - interval '40 days',
                now() - interval '39 days', 100, 10, ARRAY['a'], '{}'::jsonb,
                '{}'::jsonb, 'models/x.pkl', %s)
        """,
        (version, trained_days_ago, "a" * 64),
    )
    conn.execute("INSERT INTO decide.champion_history (model_version) VALUES (%s)", (version,))


@pytest.mark.db
def test_an_evaluation_that_fires_nothing_is_still_recorded(fresh_db: None) -> None:
    """M5-FR-11. A table holding only firings cannot answer "was this checked
    yesterday", and a trigger that silently stopped being evaluated looks
    exactly like one that keeps not firing."""
    with connect() as conn:
        _champion(conn)

    result = triggers.run(window_day=date(2026, 8, 14), retrain=False)
    assert result["fired"] is False

    with connect() as conn:
        row = conn.execute(
            "SELECT window_day, fired, n_matured FROM decide.trigger_evaluations"
        ).fetchone()
    assert row is not None
    assert row["fired"] is False


@pytest.mark.db
def test_the_floor_fires_on_its_own(fresh_db: None) -> None:
    """The trigger that does not depend on any measurement, and the one that
    keeps a quiet system from going stale unnoticed."""
    with connect() as conn:
        _champion(conn, trained_days_ago=pre.RETRAIN_FLOOR_DAYS + 1)

    result = triggers.run(window_day=date(2026, 8, 14), retrain=False)
    assert result["fired"] is True
    assert any("floor" in reason for reason in result["reasons"])


@pytest.mark.db
def test_a_gap_in_evaluations_resets_the_streak(fresh_db: None) -> None:
    """M5-FR-12, and the part most likely to be got wrong.

    Consecutive means consecutive DAYS. GitHub's cron is best-effort, so
    evaluations get missed, and reading "the last three rows" as "three
    consecutive windows" would let a trigger fire on evidence spanning a week
    that was never continuous.
    """
    day = date(2026, 8, 14)
    with connect() as conn:
        _champion(conn)
        # Two days of breach, then a four-day hole, recorded by hand so the
        # streak logic is what is under test rather than the measurement.
        for offset, streak in ((6, 1), (5, 2)):
            conn.execute(
                """
                INSERT INTO decide.trigger_evaluations
                    (window_day, champion_version, decay_breached, decay_streak, fired)
                VALUES (%s, 'champ', true, %s, false)
                """,
                (day - timedelta(days=offset), streak),
            )

    triggers.run(window_day=day, retrain=False)

    with connect() as conn:
        row = conn.execute(
            "SELECT decay_streak FROM decide.trigger_evaluations WHERE window_day = %s",
            (day,),
        ).fetchone()
    assert row["decay_streak"] == 0, "a four-day hole cannot extend a streak"


@pytest.mark.db
def test_the_same_day_is_never_evaluated_twice(fresh_db: None) -> None:
    """Two runs on one day would let a condition reach three consecutive
    windows in a single afternoon."""
    with connect() as conn:
        _champion(conn)

    triggers.run(window_day=date(2026, 8, 14), retrain=False)
    triggers.run(window_day=date(2026, 8, 14), retrain=False)

    with connect() as conn:
        row = conn.execute("SELECT count(*) AS n FROM decide.trigger_evaluations").fetchone()
    assert row["n"] == 1


def test_psi_still_works_on_a_binary_feature() -> None:
    """is_logged_out and is_mobile collapse to two bins, exactly like a constant
    does — and for them a shift in the proportion is the drift worth catching.
    An earlier guard rejected both for the same reason."""
    reference = np.concatenate([np.zeros(900), np.ones(100)])
    shifted = np.concatenate([np.zeros(500), np.ones(500)])
    value = triggers.psi(reference, shifted)
    assert value is not None and value > pre.DRIFT_PSI_THRESHOLD
