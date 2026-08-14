"""Continuous evaluation on the register (M4 §3 to §5).

The first test is the one that matters least and catches the most: it imports
the module. A syntax error in an untested module passed a green suite twice in
this project's history.
"""

from __future__ import annotations

from typing import Any

import pytest

from bellwether import metrics
from bellwether.db import connect


def test_the_module_imports() -> None:
    assert metrics.PROVISIONAL_MATURITY_SECONDS == 7 * 24 * 3600


def test_the_segment_list_is_the_one_the_spec_fixed() -> None:
    """M4-FR-12. Fixed in Bellwether_M4_Spec.md before any segmented number was
    computed. Changing it is an amendment with a date on it, not an analysis
    choice made once the results are visible."""
    assert set(metrics.SEGMENTS) == {
        "sampling_stratum",
        "editor_has_history",
        "namespace",
        "scoring_lag_bucket",
    }


def _rows(n: int, *, positives: int, weight: float = 33.3) -> list[dict[str, Any]]:
    """Scores that rank positives above negatives, so PR-AUC is well defined."""
    out = []
    for i in range(n):
        label = i < positives
        out.append(
            {
                "label": label,
                "score": 0.9 if label else 0.1,
                "sampling_weight": weight,
                "is_logged_out": label,
                "scored_late": False,
            }
        )
    return out


def test_a_window_with_one_class_publishes_the_count_not_a_number() -> None:
    """PR-AUC is undefined with no positives. Inventing one would be worse than
    publishing the gap with its n beside it (M4-FR-1)."""
    result = metrics.compute(_rows(50, positives=0))
    assert result["n"] == 50
    assert result["n_positives"] == 0
    assert result["pr_auc"] is None
    assert result["roc_auc"] is None


def test_an_empty_window_is_not_an_error() -> None:
    assert metrics.compute([])["n"] == 0


def test_every_rate_is_published_raw_and_population_weighted() -> None:
    """M1-FR-3, M4-FR-8. The frame keeps 50% of logged-out edits and 3% of
    registered ones, so these are different numbers and choosing per table
    would be exactly the quiet selection this project exists to prevent."""
    rows = _rows(10, positives=5)
    for r in rows[:5]:
        r["sampling_weight"] = 2.0  # positives sampled heavily
    for r in rows[5:]:
        r["sampling_weight"] = 33.3  # negatives barely

    result = metrics.compute(rows)
    assert result["base_rate"] == 0.5
    assert result["weighted_base_rate"] is not None
    assert result["weighted_base_rate"] < 0.1, "weighting must pull the rate toward the population"


def test_the_interval_is_published_with_the_estimate() -> None:
    result = metrics.compute(_rows(200, positives=20))
    assert result["pr_auc"] is not None
    assert result["pr_auc_ci_low"] <= result["pr_auc"] <= result["pr_auc_ci_high"]


def test_calibration_bins_are_fixed_width_and_carry_their_counts() -> None:
    """Equal-width, not quantile: the question is whether 0.9 means what it
    says, and quantile edges would move every run so two runs could not be
    compared. Empty bins are still published — a decile holding four events and
    one holding four thousand look identical without n."""
    bins = metrics.calibration(_rows(100, positives=30))
    assert len(bins) == metrics.CALIBRATION_BINS
    assert [b["bin_index"] for b in bins] == list(range(10))
    assert sum(b["n"] for b in bins) == 100
    assert any(b["n"] == 0 for b in bins), "empty bins must still appear"

    populated = [b for b in bins if b["n"]]
    assert all(b["observed_rate"] is not None for b in populated)
    assert all(b["weighted_observed_rate"] is not None for b in populated)


def test_a_score_of_exactly_one_lands_in_the_last_bin() -> None:
    """Half-open bins put 1.0 outside every one of them, which loses the most
    confident predictions the model makes."""
    rows = [
        {"label": True, "score": 1.0, "sampling_weight": 1.0, "is_logged_out": True},
        {"label": False, "score": 0.05, "sampling_weight": 1.0, "is_logged_out": False},
    ]
    bins = metrics.calibration(rows)
    assert sum(b["n"] for b in bins) == 2
    assert bins[-1]["n"] == 1


# --- against the database ---------------------------------------------------


def _event(
    conn: Any, revid: int, *, hours_ago: int, ns: int = 0, stratum: str = "registered"
) -> None:
    conn.execute(
        """
        INSERT INTO landing.rc_events
            (revid, event_ts, ns, title, user_name, user_id, is_anon, is_temp,
             is_minor, is_bot, oldlen, newlen, tags, sampling_stratum,
             sampling_weight, ingested_at_utc)
        VALUES (%s, now() - make_interval(hours => %s), %s, 'Page', %s, 500, false,
                false, false, false, 100, 120, '{}', %s, 33.3, now())
        """,
        (revid, hours_ago, ns, f"User{revid}", stratum),
    )


def _prediction(conn: Any, revid: int, *, score: float, hours_ago: int, late: bool = False) -> None:
    conn.execute(
        """
        INSERT INTO register.predictions
            (revid, event_ts, scored_at, model_version, role, score, feature_hash,
             outcome_observable_at_scoring)
        SELECT revid, event_ts, event_ts + interval '5 minutes', 'v1', 'champion',
               %s, 'h', %s
          FROM landing.rc_events WHERE revid = %s
        """,
        (score, late, revid),
    )


def _checked(conn: Any, revid: int, *, age_seconds: int, reverted: bool) -> None:
    conn.execute(
        """
        INSERT INTO outcome.label_checks
            (revid, checkpoint_seconds, checked_at_utc, age_seconds, had_reverted_tag)
        VALUES (%s, %s, now(), %s, %s)
        """,
        (revid, 7 * 24 * 3600, age_seconds, reverted),
    )


@pytest.mark.db
def test_an_unchecked_prediction_is_never_counted_as_a_negative(fresh_db: None) -> None:
    """M4-FR-6 and SRS R-3, the failure that would inflate every number here.

    Maturity is not a clock check. An edit nobody has looked at is not a
    negative — treating it as one is how the M2 rate read 22.04% when the
    checkpoint data said 38.21%.
    """
    with connect() as conn:
        _event(conn, 1, hours_ago=240)
        _prediction(conn, 1, score=0.8, hours_ago=240)
        # No label_checks row at all: nobody has looked.

        _event(conn, 2, hours_ago=240)
        _prediction(conn, 2, score=0.2, hours_ago=240)
        _checked(conn, 2, age_seconds=200 * 3600, reverted=False)

    metrics.run()

    with connect() as conn:
        row = conn.execute(
            "SELECT n FROM outcome.prediction_metrics "
            "WHERE window_label = 'all' AND segment = 'all'"
        ).fetchone()
    assert row is not None and row["n"] == 1, "only the observed one may count"


@pytest.mark.db
def test_late_scores_are_excluded_and_their_own_base_rate_is_published(fresh_db: None) -> None:
    """M4-FR-3. Excluding them is correct; they are also not a random sample.

    Predictions written after their outcome was observable concentrate in edits
    that were reverted fast, so the exclusion selects on the outcome. "We
    excluded 4%" and "we excluded 4% that were 60% positive" are different
    statements about the same number, and only the second one is checkable.
    """
    with connect() as conn:
        for revid in (1, 2, 3):
            _event(conn, revid, hours_ago=240)
            _checked(conn, revid, age_seconds=200 * 3600, reverted=revid != 3)
        _prediction(conn, 1, score=0.9, hours_ago=240, late=True)
        _prediction(conn, 2, score=0.8, hours_ago=240, late=True)
        _prediction(conn, 3, score=0.1, hours_ago=240, late=False)

    metrics.run()

    with connect() as conn:
        row = conn.execute(
            "SELECT n, excluded_late, excluded_late_base_rate FROM outcome.prediction_metrics "
            "WHERE window_label = 'all' AND segment = 'all'"
        ).fetchone()

    assert row is not None
    assert row["n"] == 1, "the two late ones are out of the accuracy figure"
    assert row["excluded_late"] == 2
    assert row["excluded_late_base_rate"] == 1.0, "both excluded rows were positives"


@pytest.mark.db
def test_every_segment_is_written_every_run(fresh_db: None) -> None:
    """M4-FR-13. Including the ones that look bad, and including the run where
    a level holds four events — which is what n is published for."""
    with connect() as conn:
        for revid in range(1, 7):
            _event(conn, revid, hours_ago=240, ns=0 if revid % 2 else 14)
            _prediction(conn, revid, score=0.9 if revid <= 3 else 0.1, hours_ago=240)
            _checked(conn, revid, age_seconds=200 * 3600, reverted=revid <= 3)

    metrics.run()

    with connect() as conn:
        rows = conn.execute("SELECT DISTINCT segment FROM outcome.prediction_metrics").fetchall()
    assert {r["segment"] for r in rows} == {"all", *metrics.SEGMENTS}


@pytest.mark.db
def test_metrics_are_append_only_by_grant(fresh_db: None) -> None:
    """M4-FR-9. A run that produced a bad number cannot be re-run away."""
    with connect() as conn:
        grants = conn.execute(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee = 'bellwether_writer' AND table_schema = 'outcome' "
            "  AND table_name = 'prediction_metrics'"
        ).fetchall()
    held = {g["privilege_type"] for g in grants}
    assert "INSERT" in held
    assert "UPDATE" not in held and "DELETE" not in held and "TRUNCATE" not in held


@pytest.mark.db
def test_a_positive_does_not_enter_the_sample_before_the_window_elapses(fresh_db: None) -> None:
    """The first production run graded 178 predictions and every one was a
    positive.

    Not a coding error — the rule said matured meant "observed for the window
    OR already known reverted", so a revert qualified the moment it was found
    while a negative had to wait the window out. At any instant the gradeable
    set was therefore the reverts. Inclusion is elapsed time since the EDIT,
    applied to both classes alike.
    """
    with connect() as conn:
        # Reverted, found early, but the edit is only a day old.
        _event(conn, 1, hours_ago=24)
        _prediction(conn, 1, score=0.9, hours_ago=24)
        _checked(conn, 1, age_seconds=3600, reverted=True)

    metrics.run()
    with connect() as conn:
        row = conn.execute(
            "SELECT n FROM outcome.prediction_metrics "
            "WHERE window_label = 'all' AND segment = 'all'"
        ).fetchone()
    assert row is not None and row["n"] == 0, "a one-day-old edit cannot be graded at seven days"


@pytest.mark.db
def test_a_revert_found_early_is_still_graded_once_the_window_passes(fresh_db: None) -> None:
    """The opposite failure, and the worse one.

    The labeller stops checking an edit once it is labelled positive, so a
    revert found at one hour has last_observed_age frozen at one hour forever.
    Requiring an observation at or beyond the window — without the elapsed-time
    arm — would exclude every early revert permanently, and the sample would
    lose exactly the events the model exists to find.
    """
    with connect() as conn:
        _event(conn, 1, hours_ago=240)
        _prediction(conn, 1, score=0.9, hours_ago=240)
        _checked(conn, 1, age_seconds=3600, reverted=True)  # never checked again

        _event(conn, 2, hours_ago=240)
        _prediction(conn, 2, score=0.1, hours_ago=240)
        _checked(conn, 2, age_seconds=200 * 3600, reverted=False)

    metrics.run()
    with connect() as conn:
        row = conn.execute(
            "SELECT n, n_positives FROM outcome.prediction_metrics "
            "WHERE window_label = 'all' AND segment = 'all'"
        ).fetchone()
    assert row is not None
    assert row["n"] == 2, "both classes must be gradeable at the same moment"
    assert row["n_positives"] == 1
