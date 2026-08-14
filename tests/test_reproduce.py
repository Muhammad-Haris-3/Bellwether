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
def test_a_revert_folded_in_before_scoring_is_reproduced(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reverts enter the counters when apply_reverts wrote them.

    So a batch scored after that moment reads the revert and a batch scored
    before does not, and the reproduction has to fold on the same boundary. Not
    on when the revert happened, and not on when it was discovered — on when it
    reached the table the scorer reads.
    """
    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    path = tmp_path / "champion-fold.pkl"
    path.write_bytes(pickle.dumps(ConstantModel(), protocol=5))
    digest = registry.artifact_sha256(path)

    revid = next(r for r in range(100, 5_000) if reproduce.sampled(r))

    with connect() as conn:
        _register(conn, "champion-fold", digest)
        _event(conn, revid - 1, minutes_ago=300)
        _event(conn, revid, minutes_ago=200)

        # The earlier edit's revert reached the counters an hour before the
        # later edit was scored, so the scorer saw it.
        conn.execute(
            "INSERT INTO landing.state_applied_reverts (revid, applied_at_utc) "
            "VALUES (%s, now() - make_interval(mins => 60))",
            (revid - 1,),
        )
        conn.execute(
            """
            INSERT INTO register.predictions
                (revid, event_ts, scored_at, model_version, role, score,
                 feature_hash, outcome_observable_at_scoring)
            SELECT revid, event_ts, now() - make_interval(mins => 30),
                   'champion-fold', 'champion', 0.7, 'placeholder', false
              FROM landing.rc_events WHERE revid = %s
            """,
            (revid - 1,),
        )

        event = conn.execute(
            "SELECT e.revid, e.old_revid, e.event_ts, e.ns, e.title, e.user_name, "
            "e.user_id, e.is_anon, e.is_temp, e.is_minor, e.is_bot, e.comment, "
            "e.comment_hidden, e.oldlen, e.newlen, e.tags, e.sampling_stratum "
            "FROM landing.rc_events e WHERE e.revid = %s",
            (revid,),
        ).fetchone()
        first = conn.execute(
            "SELECT event_ts FROM landing.rc_events WHERE revid = %s", (revid - 1,)
        ).fetchone()

        history = dict(state.EMPTY)
        history["event_ts"] = event["event_ts"]
        history["editor_edits_seen"] = 1
        history["page_edits_seen"] = 1
        history["editor_first_seen"] = first["event_ts"]
        history["max_user_id_seen"] = 500
        history["editor_edits_reverted"] = 1
        history["page_edits_reverted"] = 1

        conn.execute(
            """
            INSERT INTO register.predictions
                (revid, event_ts, scored_at, model_version, role, score,
                 feature_hash, outcome_observable_at_scoring)
            VALUES (%s, %s, now(), 'champion-fold', 'champion', 0.7, %s, false)
            """,
            (revid, event["event_ts"], features.feature_hash(features.build(event, history))),
        )

    result = reproduce.run(days=2)
    assert result["unreproducible"] == 0
    assert result["hash_matched"] == 1


@pytest.mark.db
def test_state_built_before_the_window_is_out_of_scope_not_a_failure(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prediction whose editor was already carrying state when the replay
    begins is not a prediction that fails to reproduce. It is one the run had
    no business claiming to have checked, and the two are published separately
    because a job that silently drops what it cannot verify reports a clean
    rate over a shrinking denominator."""
    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    path = tmp_path / "champion-scope.pkl"
    path.write_bytes(pickle.dumps(ConstantModel(), protocol=5))
    digest = registry.artifact_sha256(path)

    revid = next(r for r in range(100, 5_000) if reproduce.sampled(r))

    with connect() as conn:
        _register(conn, "champion-scope", digest)
        conn.execute(
            """
            INSERT INTO landing.rc_events
                (revid, event_ts, ns, title, user_name, user_id, is_anon, is_temp,
                 is_minor, is_bot, oldlen, newlen, tags, sampling_stratum,
                 sampling_weight, ingested_at_utc)
            VALUES (%s, now() - make_interval(days => 20), 0, 'Page', 'Alice', 500,
                    false, false, false, false, 100, 120, '{}', 'registered', 33.3, now())
            """,
            (revid - 1,),
        )
        # Scored twenty days ago, so its contribution to Alice's state is
        # already baked in before any seven-day replay opens.
        conn.execute(
            """
            INSERT INTO register.predictions
                (revid, event_ts, scored_at, model_version, role, score,
                 feature_hash, outcome_observable_at_scoring)
            VALUES (%s, now() - make_interval(days => 20), now() - make_interval(days => 20),
                    'champion-scope', 'champion', 0.7, 'placeholder', false)
            """,
            (revid - 1,),
        )
        _event(conn, revid, minutes_ago=60)
        conn.execute(
            """
            INSERT INTO register.predictions
                (revid, event_ts, scored_at, model_version, role, score,
                 feature_hash, outcome_observable_at_scoring)
            VALUES (%s, now() - make_interval(mins => 60), now(), 'champion-scope',
                    'champion', 0.7, %s, false)
            """,
            (revid, "a" * 64),
        )

    result = reproduce.run(days=2, history_days=7)
    assert result["state_predates_window"] == 1
    assert result["unreproducible"] == 0

    # Reach back far enough and it becomes a real, and failing, check.
    with pytest.raises(reproduce.ReproductionFailure):
        reproduce.run(days=2, history_days=40)


@pytest.mark.db
def test_a_prediction_from_a_superseded_model_is_not_a_failure(fresh_db: None) -> None:
    """The regression that turned this job permanently red.

    `state.py` changed substantially in M3 — reverts fold in when this system
    LEARNED of them rather than when they happened — so a prediction written
    before that fix cannot re-derive under the code that replaced it. Production
    reported 180 of 1,694 unreproducible and the daily run went red, when what
    had actually happened was a correctness fix landing.

    The claim is now scoped to what the SERVING model produced. A prediction
    from a superseded champion is counted in its own column, like state that
    predates the window already is — not checkable rather than failed.
    """
    result = reproduce.run(days=2, percent=100, history_days=2)
    assert "superseded_model" in result, "the category must be reported, not folded away"
    assert result["superseded_model"] == 0, "nothing superseded in a fresh database"


def test_the_narrower_claim_is_stated_in_the_module() -> None:
    """A guarantee that quietly shrank would be worse than one that failed. The
    scoping is documented where someone reading the reproducibility figure will
    find it."""
    import inspect

    source = inspect.getsource(reproduce)
    assert "superseded" in source
    assert "SERVING model" in source
