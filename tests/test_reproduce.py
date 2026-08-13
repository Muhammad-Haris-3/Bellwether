"""Re-deriving stored predictions (M3-FR-17, M3-FR-18)."""

from __future__ import annotations

import pickle
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bellwether import features, registry, reproduce, state
from bellwether.db import connect

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


class ConstantModel:
    """Module scope, because pickle cannot serialise a class declared inside a
    function and the loader here unpickles exactly as the scorer does."""

    def predict_proba(self, rows: Any) -> Any:
        return [[0.3, 0.7] for _ in rows]


def _event(conn: Any, revid: int, *, minutes_ago: int, user: str = "Alice") -> None:
    conn.execute(
        """
        INSERT INTO landing.rc_events
            (revid, event_ts, ns, title, user_name, user_id, is_anon, is_temp,
             is_minor, is_bot, oldlen, newlen, tags, sampling_stratum,
             sampling_weight, ingested_at_utc)
        VALUES (%s, now() - make_interval(mins => %s), 0, 'Page', %s, 500, false,
                false, false, false, 100, 120, '{}', 'registered', 33.3, now())
        """,
        (revid, minutes_ago, user),
    )


def _register(conn: Any, version: str, sha: str) -> None:
    conn.execute(
        """
        INSERT INTO register.model_registry
            (model_version, training_start, training_end, n_train_events,
             n_train_positives, feature_names, hyperparameters, offline_metrics,
             artifact_path, artifact_sha256)
        VALUES (%s, %s, %s, 100, 10, ARRAY['a'], '{}'::jsonb, '{}'::jsonb, 'models/x.pkl', %s)
        """,
        (version, NOW - timedelta(days=2), NOW - timedelta(days=1), sha),
    )


def test_the_sample_is_deterministic() -> None:
    """Sampling at random would make a falling agreement rate indistinguishable
    from a different draw."""
    once = [r for r in range(5_000) if reproduce.sampled(r)]
    twice = [r for r in range(5_000) if reproduce.sampled(r)]
    assert once == twice
    assert 100 < len(once) < 500, "roughly 5% of 5,000"


@pytest.mark.db
def test_a_faithfully_written_prediction_reproduces(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole claim of M3-FR-17, end to end: score through the real scorer,
    then re-derive from the raw events and require the same hash and the same
    score."""
    from bellwether import score

    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    path = tmp_path / "champion-repro.pkl"
    path.write_bytes(pickle.dumps(ConstantModel(), protocol=5))
    digest = registry.artifact_sha256(path)

    with connect() as conn:
        for revid in range(1, 400):
            _event(conn, revid, minutes_ago=600 - revid, user=f"User{revid % 7}")
        _register(conn, "champion-repro", digest)

    assert score.run(limit=1_000)["scored"] == 399

    result = reproduce.run(days=2)
    assert result["sampled"] > 0, "the sample must actually catch something"
    assert result["unreproducible"] == 0
    assert result["hash_matched"] == result["sampled"]
    assert result["score_matched"] == result["sampled"]
    assert result["agreement"] == 1.0


@pytest.mark.db
def test_the_run_is_recorded_as_evidence(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3-FR-18 publishes a rate. A rate nobody stored is a print statement."""
    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    reproduce.run(days=2)

    with connect() as conn:
        row = conn.execute(
            "SELECT sampled, hash_matched, unreproducible FROM register.reproductions"
        ).fetchone()
    assert row is not None and row["sampled"] == 0


@pytest.mark.db
def test_a_prediction_that_cannot_be_re_derived_fails_loudly(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored hash that no state can produce is a broken FR-17, and the job
    says so rather than reporting a slightly lower percentage."""
    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    path = tmp_path / "champion-bad.pkl"
    path.write_bytes(pickle.dumps(ConstantModel(), protocol=5))
    digest = registry.artifact_sha256(path)

    with connect() as conn:
        _register(conn, "champion-bad", digest)
        revid = next(r for r in range(1, 5_000) if reproduce.sampled(r))
        _event(conn, revid, minutes_ago=120)
        conn.execute(
            """
            INSERT INTO register.predictions
                (revid, event_ts, scored_at, model_version, role, score,
                 feature_hash, outcome_observable_at_scoring)
            VALUES (%s, now() - make_interval(mins => 120), now(), 'champion-bad',
                    'champion', 0.7, %s, false)
            """,
            (revid, "f" * 64),
        )

    with pytest.raises(reproduce.ReproductionFailure, match="could not be re-derived"):
        reproduce.run(days=2)


@pytest.mark.db
def test_a_revert_learned_between_the_edit_and_its_scoring_is_diagnosed(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The distinction the second hash exists to draw.

    The scorer reads persisted state as of the moment it runs, not as of the
    edit. A revert discovered in between is in its view and not in training's,
    so the hash will not match under the training-time definition — and that is
    a measurable statement about train/serve skew, not an unreproducible
    prediction.
    """
    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    path = tmp_path / "champion-skew.pkl"
    path.write_bytes(pickle.dumps(ConstantModel(), protocol=5))
    digest = registry.artifact_sha256(path)

    revid = next(r for r in range(100, 5_000) if reproduce.sampled(r))

    with connect() as conn:
        _register(conn, "champion-skew", digest)
        # An earlier edit by the same author, reverted, and discovered only
        # AFTER the edit we are about to check was made.
        _event(conn, revid - 1, minutes_ago=300)
        _event(conn, revid, minutes_ago=200)
        conn.execute(
            """
            INSERT INTO outcome.revert_events
                (revert_revid, reverted_revid, revert_ts, method, observed_at_utc)
            VALUES (%s, %s, now() - make_interval(mins => 250), 'mw-undo',
                    now() - make_interval(mins => 100))
            """,
            (revid + 5_000, revid - 1),
        )

        # The hash the scorer would have written, having seen that revert.
        event = conn.execute(
            "SELECT e.revid, e.old_revid, e.event_ts, e.ns, e.title, e.user_name, "
            "e.user_id, e.is_anon, e.is_temp, e.is_minor, e.is_bot, e.comment, "
            "e.comment_hidden, e.oldlen, e.newlen, e.tags, e.sampling_stratum "
            "FROM landing.rc_events e WHERE e.revid = %s",
            (revid,),
        ).fetchone()
        history = dict(state.EMPTY)
        history["event_ts"] = event["event_ts"]
        history["editor_edits_seen"] = 1
        history["page_edits_seen"] = 1
        history["editor_first_seen"] = event["event_ts"] - timedelta(minutes=100)
        history["max_user_id_seen"] = 500
        history["editor_edits_reverted"] = 1
        history["page_edits_reverted"] = 1
        late_hash = features.feature_hash(features.build(event, history))

        conn.execute(
            """
            INSERT INTO register.predictions
                (revid, event_ts, scored_at, model_version, role, score,
                 feature_hash, outcome_observable_at_scoring)
            VALUES (%s, %s, now(), 'champion-skew', 'champion', 0.7, %s, false)
            """,
            (revid, event["event_ts"], late_hash),
        )

    result = reproduce.run(days=2)
    assert result["unreproducible"] == 0, "it IS reproducible, under the scorer's definition"
    assert result["matched_at_scoring_time"] == 1
    assert result["hash_matched"] == 0
