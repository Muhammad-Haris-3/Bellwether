"""Shadow scoring (M5-FR-7 to FR-10).

A challenger scores everything the champion scores, in the same run, from the
same state — and is never served, never counted, and never penalised for
failing.
"""

from __future__ import annotations

import pickle
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bellwether import registry, score
from bellwether.db import connect

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


class ConstantModel:
    def __init__(self, probability: float = 0.7) -> None:
        self.probability = probability

    def predict_proba(self, rows: Any) -> Any:
        return [[1.0 - self.probability, self.probability] for _ in rows]


class BrokenModel:
    """Scores nothing. Stands in for a challenger with a feature-shape bug —
    the realistic failure, since a challenger is trained by the same code but
    may carry a different feature list."""

    def predict_proba(self, rows: Any) -> Any:
        raise ValueError("shape mismatch")


def _event(conn: Any, revid: int, *, minutes_ago: int) -> None:
    conn.execute(
        """
        INSERT INTO landing.rc_events
            (revid, event_ts, ns, title, user_name, user_id, is_anon, is_temp,
             is_minor, is_bot, oldlen, newlen, tags, sampling_stratum,
             sampling_weight, ingested_at_utc)
        VALUES (%s, now() - make_interval(mins => %s), 0, 'Page', 'Alice', 500,
                false, false, false, false, 100, 120, '{}', 'registered', 33.3, now())
        """,
        (revid, minutes_ago),
    )


def _register(conn: Any, version: str, sha: str, *, days_ago: int = 1) -> None:
    conn.execute(
        """
        INSERT INTO register.model_registry
            (model_version, trained_at, training_start, training_end, n_train_events,
             n_train_positives, feature_names, hyperparameters, offline_metrics,
             artifact_path, artifact_sha256)
        VALUES (%s, now() - make_interval(days => %s), %s, %s, 100, 10, ARRAY['a'],
                '{}'::jsonb, '{}'::jsonb, 'models/x.pkl', %s)
        """,
        (version, days_ago, NOW - timedelta(days=9), NOW - timedelta(days=8), sha),
    )


def _artifact(tmp_path: Any, name: str, model: Any) -> str:
    path = tmp_path / f"{name}.pkl"
    path.write_bytes(pickle.dumps(model, protocol=5))
    return registry.artifact_sha256(path)


@pytest.mark.db
def test_the_challenger_scores_the_same_events_from_the_same_state(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M5-FR-7 and FR-9. One scorer, two model versions.

    Train/serve skew took three modules to find in M3 because the same fold was
    implemented twice. A separate shadow scorer would be that mistake made on
    purpose.
    """
    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    champ_sha = _artifact(tmp_path, "champ", ConstantModel(0.7))
    chal_sha = _artifact(tmp_path, "chal", ConstantModel(0.4))

    with connect() as conn:
        for revid in range(1, 6):
            _event(conn, revid, minutes_ago=100 + revid)
        _register(conn, "champ", champ_sha, days_ago=5)
        _register(conn, "chal", chal_sha, days_ago=1)
        conn.execute("INSERT INTO decide.champion_history (model_version) VALUES ('champ')")

    score.run(limit=100)

    with connect() as conn:
        rows = conn.execute(
            "SELECT role, count(*) AS n, count(DISTINCT feature_hash) AS hashes "
            "FROM register.predictions GROUP BY role ORDER BY role"
        ).fetchall()
        paired = conn.execute(
            "SELECT count(*) AS n FROM register.predictions a "
            "  JOIN register.predictions b ON b.revid = a.revid "
            " WHERE a.role = 'champion' AND b.role = 'shadow' "
            "   AND a.feature_hash = b.feature_hash"
        ).fetchone()

    # The role VALUE is 'shadow' — the state the model scores in. The model is
    # the challenger. M3-FR-7 reserved exactly this value so the evidential
    # table would need no migration when M5 arrived, and it held.
    by_role = {r["role"]: r for r in rows}
    assert by_role["champion"]["n"] == 5
    assert by_role["shadow"]["n"] == 5
    # The same feature vector produced both scores. If these differed, the
    # paired comparison the promotion rule runs would be comparing two models
    # on two different views of the same event.
    assert paired["n"] == 5


@pytest.mark.db
def test_a_challenger_that_cannot_score_loses_nothing(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M5-FR-10. The event is dropped from the pairing, not counted against it.

    Scoring a failure as a loss would let an unstable challenger hide behind a
    mediocre metric, and would make the champion look better the more often the
    challenger broke.
    """
    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    champ_sha = _artifact(tmp_path, "champ", ConstantModel(0.7))
    chal_sha = _artifact(tmp_path, "chal", BrokenModel())

    with connect() as conn:
        for revid in range(1, 4):
            _event(conn, revid, minutes_ago=100 + revid)
        _register(conn, "champ", champ_sha, days_ago=5)
        _register(conn, "chal", chal_sha, days_ago=1)
        conn.execute("INSERT INTO decide.champion_history (model_version) VALUES ('champ')")

    result = score.run(limit=100)
    assert result["scored"] == 3, "the champion is unaffected by the challenger failing"

    with connect() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM register.predictions WHERE role = 'shadow'"
        ).fetchone()
    assert row["n"] == 0, "no challenger row, and no zero-score row either"


@pytest.mark.db
def test_the_champion_scores_alone_when_there_is_no_challenger(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    champ_sha = _artifact(tmp_path, "champ", ConstantModel(0.7))

    with connect() as conn:
        _event(conn, 1, minutes_ago=100)
        _register(conn, "champ", champ_sha, days_ago=5)
        conn.execute("INSERT INTO decide.champion_history (model_version) VALUES ('champ')")

    score.run(limit=100)

    with connect() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM register.predictions WHERE role = 'shadow'"
        ).fetchone()
    assert row["n"] == 0


@pytest.mark.db
def test_a_rolled_back_model_is_not_put_back_into_shadow(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M5-FR-27. Re-promoting it would re-read the identical shadow record that
    promoted it the first time, which is not new evidence."""
    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    with connect() as conn:
        _register(conn, "champ", "a" * 64, days_ago=5)
        _register(conn, "chal", "b" * 64, days_ago=1)
        conn.execute("INSERT INTO decide.champion_history (model_version) VALUES ('champ')")

        assert registry.challenger(conn, "champ")["model_version"] == "chal"

        conn.execute(
            """
            INSERT INTO decide.model_decisions
                (decision, champion_version, challenger_version)
            VALUES ('rollback', 'champ', 'chal')
            """
        )
        assert registry.challenger(conn, "champ") is None
