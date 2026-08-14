"""Which model is serving (M5-FR-24).

M3 answered "most recently registered" and named it a placeholder. These tests
are about why that answer becomes actively wrong the moment M5 runs, rather
than merely approximate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bellwether import registry
from bellwether.db import connect

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


def _register(conn: Any, version: str, *, trained_days_ago: int) -> None:
    conn.execute(
        """
        INSERT INTO register.model_registry
            (model_version, trained_at, training_start, training_end, n_train_events,
             n_train_positives, feature_names, hyperparameters, offline_metrics,
             artifact_path, artifact_sha256)
        VALUES (%s, now() - make_interval(days => %s), %s, %s, 100, 10, ARRAY['a'],
                '{}'::jsonb, '{}'::jsonb, 'models/x.pkl', %s)
        """,
        (version, trained_days_ago, NOW - timedelta(days=9), NOW - timedelta(days=8), "a" * 64),
    )


def _promote(conn: Any, version: str, *, replaced: str | None = None) -> None:
    conn.execute(
        "INSERT INTO decide.champion_history (model_version, replaced) VALUES (%s, %s)",
        (version, replaced),
    )


@pytest.mark.db
def test_before_any_promotion_the_newest_model_serves(fresh_db: None) -> None:
    """There is genuinely nothing else to answer with, and a scorer that
    refuses to run because no decision has been made yet would have blocked all
    of M3."""
    with connect() as conn:
        _register(conn, "champion-old", trained_days_ago=3)
        _register(conn, "champion-new", trained_days_ago=1)
        assert registry.champion(conn)["model_version"] == "champion-new"


@pytest.mark.db
def test_a_newly_trained_challenger_does_not_take_production(fresh_db: None) -> None:
    """The reason the M3 rule had to go.

    A challenger is registered the instant it is trained. Under "most recently
    registered" it would be serving before it had proved anything — which is
    precisely what the pre-registered promotion rule exists to prevent, and it
    would have happened silently on the first automatic retrain.
    """
    with connect() as conn:
        _register(conn, "champion-1", trained_days_ago=5)
        _promote(conn, "champion-1")
        _register(conn, "challenger-2", trained_days_ago=0)

        assert registry.champion(conn)["model_version"] == "champion-1"


@pytest.mark.db
def test_a_promotion_moves_production(fresh_db: None) -> None:
    with connect() as conn:
        _register(conn, "champion-1", trained_days_ago=5)
        _register(conn, "challenger-2", trained_days_ago=1)
        _promote(conn, "champion-1")
        _promote(conn, "challenger-2", replaced="champion-1")

        assert registry.champion(conn)["model_version"] == "challenger-2"


@pytest.mark.db
def test_a_rollback_restores_the_previous_champion(fresh_db: None) -> None:
    """A rollback is an ordinary row, not a deletion. The promotion it undoes
    stays in the history — a log that erased reversed decisions would answer
    "what is serving" and lose "what was tried"."""
    with connect() as conn:
        _register(conn, "champion-1", trained_days_ago=5)
        _register(conn, "challenger-2", trained_days_ago=1)
        _promote(conn, "champion-1")
        _promote(conn, "challenger-2", replaced="champion-1")
        _promote(conn, "champion-1", replaced="challenger-2")

        assert registry.champion(conn)["model_version"] == "champion-1"

        rows = conn.execute(
            "SELECT model_version FROM decide.champion_history ORDER BY history_id"
        ).fetchall()
    assert [r["model_version"] for r in rows] == ["champion-1", "challenger-2", "champion-1"]


@pytest.mark.db
def test_the_decision_log_cannot_be_edited_by_the_writer(fresh_db: None) -> None:
    """M5-FR-29. A decision that could be revised afterwards is a decision
    nobody has to stand by."""
    with connect() as conn:
        grants = conn.execute(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee = 'bellwether_writer' AND table_schema = 'decide' "
            "  AND table_name = 'model_decisions'"
        ).fetchall()
    held = {g["privilege_type"] for g in grants}
    assert "INSERT" in held
    assert "UPDATE" not in held and "DELETE" not in held and "TRUNCATE" not in held
