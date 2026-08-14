"""Rollback (M5-FR-25 to FR-28).

A system that can promote but never retreat has only half a mechanism.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from bellwether import preregistration as pre
from bellwether import promote
from bellwether.db import connect

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


def _model(conn: Any, version: str, *, days_ago: int) -> None:
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
        (version, days_ago, "a" * 64),
    )


def _promotion(conn: Any, *, old: str, new: str, old_level: float, days_ago: float) -> None:
    conn.execute(
        """
        INSERT INTO decide.model_decisions
            (decision, decided_at, champion_version, challenger_version, champion_pr_auc,
             challenger_pr_auc, p1_pass, p2_pass, p3_pass, p4_pass, p5_pass)
        VALUES ('promote', now() - make_interval(secs => %s), %s, %s, %s, %s,
                true, true, true, true, true)
        """,
        (int(days_ago * 86400), old, new, old_level, old_level + 0.05),
    )
    conn.execute(
        "INSERT INTO decide.champion_history (model_version, replaced) VALUES (%s, %s)",
        (new, old),
    )


def _scored(conn: Any, version: str, *, n: int, positives: int, score_for_positive: float) -> None:
    """Matured champion predictions, deliberately mis-ranked so the rolling
    PR-AUC lands where the test needs it."""
    for i in range(n):
        label = i < positives
        conn.execute(
            """
            INSERT INTO landing.rc_events
                (revid, event_ts, ns, title, user_name, user_id, is_anon, is_temp,
                 is_minor, is_bot, oldlen, newlen, tags, sampling_stratum,
                 sampling_weight, ingested_at_utc)
            VALUES (%s, now() - make_interval(days => 8), 0, 'Page', 'Alice', 500,
                    false, false, false, false, 100, 120, '{}', 'registered', 33.3, now())
            """,
            (i + 1,),
        )
        conn.execute(
            """
            INSERT INTO outcome.label_checks
                (revid, checkpoint_seconds, checked_at_utc, age_seconds, had_reverted_tag)
            VALUES (%s, %s, now(), %s, %s)
            """,
            (i + 1, 7 * 24 * 3600, 8 * 24 * 3600, label),
        )
        conn.execute(
            """
            INSERT INTO register.predictions
                (revid, event_ts, scored_at, model_version, role, score, feature_hash,
                 outcome_observable_at_scoring)
            SELECT revid, event_ts, event_ts + interval '5 minutes', %s, 'champion',
                   %s, 'h', false
              FROM landing.rc_events WHERE revid = %s
            """,
            (version, score_for_positive if label else 0.5, i + 1),
        )


@pytest.mark.db
def test_nothing_promoted_means_nothing_to_roll_back(fresh_db: None) -> None:
    assert promote.check_rollback()["skipped"] is True


@pytest.mark.db
def test_a_promotion_outside_the_window_is_left_alone(fresh_db: None) -> None:
    """M5-FR-28. Fourteen days from the DECISION — after that the model owns
    its performance and a decline is a decay trigger, not a rollback."""
    with connect() as conn:
        _model(conn, "old", days_ago=40)
        _model(conn, "new", days_ago=30)
        _promotion(
            conn,
            old="old",
            new="new",
            old_level=0.60,
            days_ago=pre.ROLLBACK_WINDOW_DAYS + 1,
        )
        _scored(conn, "new", n=200, positives=40, score_for_positive=0.1)

    result = promote.check_rollback()
    assert result["skipped"] is True
    assert result["reason"] == "outside window"


@pytest.mark.db
def test_a_promoted_model_that_falls_is_restored_automatically(fresh_db: None) -> None:
    """M5-FR-25 and FR-26. No human action, and an ordinary decision row.

    The promoted model ranks positives BELOW negatives here, so its rolling
    PR-AUC sits far under the level its predecessor was registered at.
    """
    with connect() as conn:
        _model(conn, "old", days_ago=20)
        _model(conn, "new", days_ago=10)
        _promotion(conn, old="old", new="new", old_level=0.90, days_ago=2)
        _scored(conn, "new", n=200, positives=40, score_for_positive=0.1)

    result = promote.check_rollback()
    assert result["rolled_back"] is True
    assert result["restored"] == "old"
    assert result["drop"] > pre.ROLLBACK_PR_AUC_DROP

    with connect() as conn:
        decision = conn.execute(
            "SELECT decision, champion_version, challenger_version, trigger_reason "
            "FROM decide.model_decisions WHERE decision = 'rollback'"
        ).fetchone()
        serving = conn.execute(
            "SELECT model_version FROM decide.champion_history "
            "ORDER BY effective_from DESC, history_id DESC LIMIT 1"
        ).fetchone()

    # Not an error state: an ordinary row, carrying why.
    assert decision["decision"] == "rollback"
    assert decision["champion_version"] == "old"
    assert decision["challenger_version"] == "new"
    assert "below" in decision["trigger_reason"]
    assert serving["model_version"] == "old"


@pytest.mark.db
def test_a_promoted_model_holding_up_is_not_disturbed(fresh_db: None) -> None:
    with connect() as conn:
        _model(conn, "old", days_ago=20)
        _model(conn, "new", days_ago=10)
        # Its predecessor was registered at a level this model comfortably beats.
        _promotion(conn, old="old", new="new", old_level=0.30, days_ago=2)
        _scored(conn, "new", n=200, positives=40, score_for_positive=0.99)

    result = promote.check_rollback()
    assert result["rolled_back"] is False

    with connect() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM decide.model_decisions WHERE decision = 'rollback'"
        ).fetchone()
    assert n["n"] == 0


@pytest.mark.db
def test_too_little_matured_evidence_is_not_a_rollback(fresh_db: None) -> None:
    """A window with one class is not evidence the promoted model is failing.
    Withdrawing on it would make a quiet week look like a broken model."""
    with connect() as conn:
        _model(conn, "old", days_ago=20)
        _model(conn, "new", days_ago=10)
        _promotion(conn, old="old", new="new", old_level=0.90, days_ago=2)
        _scored(conn, "new", n=50, positives=0, score_for_positive=0.1)

    result = promote.check_rollback()
    assert result["skipped"] is True
    assert result["reason"] == "undefined"
