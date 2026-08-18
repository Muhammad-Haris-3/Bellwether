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

from bellwether import features, registry, score
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
        VALUES (%s, now() - make_interval(days => %s), %s, %s, 100, 10, %s,
                '{}'::jsonb, '{}'::jsonb, 'models/x.pkl', %s)
        """,
        # The REAL feature list. The scorer selects each model's input columns
        # by its registered names, so a placeholder here would make the shadow
        # look incompatible and get dropped before it scored anything.
        (
            version,
            days_ago,
            NOW - timedelta(days=9),
            NOW - timedelta(days=8),
            features.feature_names(),
            sha,
        ),
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


# ---------------------------------------------------------------------------
# Feature-set incompatibility, which is not the same as instability
# ---------------------------------------------------------------------------
#
# The per-event handler above exists for a challenger that breaks on some
# inputs. A model this build cannot feed AT ALL is a different fault with a
# different fix, and reporting it through the same channel buried it: five
# thousand "no opinion" events look like a flaky model, not a deployment error.


def _register_with_features(
    conn: Any, version: str, sha: str, names: list[str], *, days_ago: int = 1
) -> None:
    conn.execute(
        """
        INSERT INTO register.model_registry
            (model_version, trained_at, training_start, training_end, n_train_events,
             n_train_positives, feature_names, hyperparameters, offline_metrics,
             artifact_path, artifact_sha256)
        VALUES (%s, now() - make_interval(days => %s), %s, %s, 100, 10, %s,
                '{}'::jsonb, '{}'::jsonb, 'models/x.pkl', %s)
        """,
        (version, days_ago, NOW - timedelta(days=9), NOW - timedelta(days=8), names, sha),
    )


@pytest.mark.db
def test_a_champion_needing_an_absent_feature_scores_nothing(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse, rather than improvise.

    The register cannot delete a row. Scoring the champion against a feature set
    it was not trained on writes numbers that are wrong into evidence that is
    permanent, and a failed run is recoverable in a way that is not.
    """
    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    champ_sha = _artifact(tmp_path, "champ", ConstantModel(0.7))

    with connect() as conn:
        for revid in range(1, 4):
            _event(conn, revid, minutes_ago=100 + revid)
        _register_with_features(
            conn, "champ", champ_sha, [*features.feature_names(), "a_feature_from_the_future"]
        )
        conn.execute("INSERT INTO decide.champion_history (model_version) VALUES ('champ')")

    result = score.run(limit=100)
    assert result.get("skipped") is True
    assert result.get("reason") == "champion feature mismatch"

    with connect() as conn:
        row = conn.execute("SELECT count(*) AS n FROM register.predictions").fetchone()
    assert row["n"] == 0, "nothing may be written when the champion cannot be fed"


@pytest.mark.db
def test_an_incompatible_challenger_is_dropped_and_the_champion_still_scores(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The challenger's problem must not become the champion's.

    Dropped once, before the loop, rather than failing per event: the count of
    "no opinion" events is how an unstable challenger is diagnosed, and filling
    it with a deployment error destroys that signal.
    """
    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    champ_sha = _artifact(tmp_path, "champ", ConstantModel(0.7))
    chal_sha = _artifact(tmp_path, "chal", ConstantModel(0.4))

    with connect() as conn:
        for revid in range(1, 4):
            _event(conn, revid, minutes_ago=100 + revid)
        _register_with_features(conn, "champ", champ_sha, features.feature_names(), days_ago=5)
        _register_with_features(
            conn, "chal", chal_sha, [*features.feature_names(), "not_built_by_this_code"]
        )
        conn.execute("INSERT INTO decide.champion_history (model_version) VALUES ('champ')")

    result = score.run(limit=100)
    assert result["scored"] == 3, "the champion is unaffected"

    with connect() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM register.predictions WHERE role = 'shadow'"
        ).fetchone()
    assert row["n"] == 0, "no shadow rows from a model that was never fed"


@pytest.mark.db
def test_champion_and_challenger_may_hold_different_feature_sets(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the whole change, and what PREREGISTRATION §11 promises.

    A challenger trained on a SUBSET still scores here, from the same vector, in
    the same run. Before this, any difference at all meant the challenger raised
    on every event and accumulated no paired observations, so P-3 could never be
    reached and the promotion rule could never fire.
    """
    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    champ_sha = _artifact(tmp_path, "champ", ConstantModel(0.7))
    chal_sha = _artifact(tmp_path, "chal", ConstantModel(0.4))

    narrower = [n for n in features.feature_names() if n != "is_minor"]

    with connect() as conn:
        for revid in range(1, 4):
            _event(conn, revid, minutes_ago=100 + revid)
        _register_with_features(conn, "champ", champ_sha, features.feature_names(), days_ago=5)
        _register_with_features(conn, "chal", chal_sha, narrower)
        conn.execute("INSERT INTO decide.champion_history (model_version) VALUES ('champ')")

    result = score.run(limit=100)
    # ROWS, not events: three events scored by two models. score.py reports it
    # this way on purpose — with a challenger in shadow there are two rows per
    # event, and calling six "scored with champ" would double the champion's
    # apparent throughput.
    assert result["scored"] == 6

    with connect() as conn:
        rows = conn.execute(
            "SELECT role, count(*) AS n, count(DISTINCT feature_hash) AS hashes"
            "  FROM register.predictions GROUP BY role ORDER BY role"
        ).fetchall()

    counts = {r["role"]: r["n"] for r in rows}
    assert counts == {"champion": 3, "shadow": 3}, "both models scored every event"

    with connect() as conn:
        pair = conn.execute(
            "SELECT count(DISTINCT feature_hash) AS distinct_hashes"
            "  FROM register.predictions WHERE revid = 1"
        ).fetchone()
    # Different inputs, so different digests — each row's hash describes what
    # its own model consumed, which is what keeps both reproducible.
    assert pair["distinct_hashes"] == 2


# ---------------------------------------------------------------------------
# Folding an event into state exactly once
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_a_re_scored_event_is_not_folded_into_state_twice(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The doubling reconcile measured, reproduced and then prevented.

    UNSCORED_SQL gates on model_version, so a champion change makes every event
    in the lookback window eligible again for the new champion — correctly, the
    new model does need predictions for them. What must not repeat is the fold.
    reconcile found editor.edits at exactly double ("replay says 5, stored says
    10") across the single promotion this project has had, because observe ran
    again and persist wrote an absolute count seeded from the inflated row.

    The prediction insert is idempotent, which is why this was invisible: the
    second pass writes no duplicate evidence, only duplicate state.
    """
    from bellwether.db import connect as db_connect

    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    first_sha = _artifact(tmp_path, "champ1", ConstantModel(0.7))
    second_sha = _artifact(tmp_path, "champ2", ConstantModel(0.6))

    with connect() as conn:
        for revid in range(1, 6):
            _event(conn, revid, minutes_ago=100 + revid)
        _register(conn, "champ1", first_sha, days_ago=5)
        conn.execute("INSERT INTO decide.champion_history (model_version) VALUES ('champ1')")

    score.run(limit=100)

    with db_connect() as conn:
        row = conn.execute(
            "SELECT edits_seen FROM landing.editor_state WHERE user_key = 'Alice'"
        ).fetchone()
    assert row["edits_seen"] == 5, "the five events were folded once"

    # A new champion is promoted. Every event becomes unscored for it.
    with connect() as conn:
        _register(conn, "champ2", second_sha, days_ago=0)
        conn.execute("INSERT INTO decide.champion_history (model_version) VALUES ('champ2')")

    score.run(limit=100)

    with db_connect() as conn:
        row = conn.execute(
            "SELECT edits_seen FROM landing.editor_state WHERE user_key = 'Alice'"
        ).fetchone()
        scored = conn.execute(
            "SELECT count(*) AS n FROM register.predictions WHERE model_version = 'champ2'"
        ).fetchone()
        ledger = conn.execute("SELECT count(*) AS n FROM landing.state_applied_events").fetchone()

    assert row["edits_seen"] == 5, "still five: the second pass must not fold them again"
    assert scored["n"] == 5, "but the new champion did score every event"
    assert ledger["n"] == 5, "one ledger row per event, not per scoring pass"


@pytest.mark.db
def test_the_ledger_survives_a_rerun_without_double_recording(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording is ON CONFLICT DO NOTHING, so a retry is not an error.

    Two writers racing on the same event is the situation this prevents; the
    loser has nothing to complain about, because the fold it was about to
    duplicate is already recorded.
    """
    from bellwether import state
    from bellwether.db import connect as db_connect

    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    with db_connect() as conn:
        assert state.record_folded(conn, [10, 11, 12]) == 3
        assert state.record_folded(conn, [11, 12, 13]) == 1, "only the new one is added"
        assert state.already_folded(conn, [10, 11, 12, 13, 14]) == {10, 11, 12, 13}
        assert state.already_folded(conn, []) == set()
        assert state.record_folded(conn, []) == 0
