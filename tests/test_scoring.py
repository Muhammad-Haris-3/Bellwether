"""Training, the registry, and the scorer.

Most of these are about refusals: what the scorer declines to do, and what the
registry declines to load.
"""

from __future__ import annotations

import pickle
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bellwether import features, registry, state
from bellwether.db import connect

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


class ConstantModel:
    """A stand-in champion that returns a fixed probability.

    Defined at module scope because pickle cannot serialise a class declared
    inside a function — and the scorer loads its model by unpickling, so the
    stub has to survive the same round trip the real artifact does.
    """

    def __init__(self, probability: float = 0.7) -> None:
        self.probability = probability

    def predict_proba(self, rows: Any) -> Any:
        return [[1.0 - self.probability, self.probability] for _ in rows]


# --- registry --------------------------------------------------------------


def test_a_tampered_artifact_is_refused(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """M3-FR-16, and the reason it is checked before loading rather than after.

    A pickled model is tied to its library version, and an artifact that does
    not match the registry would not raise on load — it would score
    differently. Verifying the digest turns a silent behaviour change into a
    refusal.
    """
    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    path = tmp_path / "champion-x.pkl"
    path.write_bytes(pickle.dumps({"model": "original"}, protocol=5))
    digest = registry.artifact_sha256(path)

    assert registry.verify("champion-x", digest) == path

    path.write_bytes(pickle.dumps({"model": "swapped"}, protocol=5))
    with pytest.raises(registry.ArtifactMismatch, match="Refusing to score"):
        registry.verify("champion-x", digest)


def test_a_missing_artifact_is_refused(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    with pytest.raises(registry.ArtifactMismatch, match="does not exist"):
        registry.verify("champion-absent", "0" * 64)


def test_the_metric_card_is_byte_stable(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sorted keys and a trailing newline, so a diff in `models/` means
    something changed rather than that a dictionary iterated differently."""
    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    card = {"b": 2, "a": 1, "features": ["z", "y"]}
    first = registry.write_card("v", card).read_bytes()
    second = registry.write_card("v", {"a": 1, "features": ["z", "y"], "b": 2}).read_bytes()
    assert first == second


# --- the scorer ------------------------------------------------------------


def _event(conn: Any, revid: int, *, minutes_ago: int = 30, user: str = "Alice") -> None:
    conn.execute(
        """
        INSERT INTO landing.rc_events
            (revid, event_ts, ns, title, user_name, user_id, is_anon, is_temp,
             is_minor, is_bot, oldlen, newlen, tags, sampling_stratum,
             sampling_weight, ingested_at_utc)
        VALUES (%s, now() - make_interval(mins => %s), 0, 'Page', %s, 500,
                false, false, false, false, 100, 120, '{}', 'registered', 33.3, now())
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
        VALUES (%s, %s, %s, 100, 10, %s, '{}'::jsonb, '{}'::jsonb, 'models/x.pkl', %s)
        """,
        # The REAL feature list, not a placeholder.
        #
        # The scorer now selects each model's input columns by its registered
        # names, so this column is load-bearing rather than decorative.
        # ARRAY['a'] stood here while nothing read it, and a fixture that lies
        # about what a model consumes can only exercise the path where that
        # does not matter.
        (
            version,
            NOW - timedelta(days=2),
            NOW - timedelta(days=1),
            features.feature_names(),
            sha,
        ),
    )


@pytest.mark.db
def test_the_champion_is_the_most_recently_registered(fresh_db: None) -> None:
    """M3's placeholder rule, named as one in sql/013. M5 replaces it with the
    pre-registered promotion rule, decided by evidence rather than recency."""
    with connect() as conn:
        _register(conn, "champion-old", "a" * 64)
        conn.execute(
            "UPDATE register.model_registry SET trained_at = now() - interval '2 days' "
            "WHERE model_version = 'champion-old'"
        )
        _register(conn, "champion-new", "b" * 64)
        assert registry.champion(conn)["model_version"] == "champion-new"


@pytest.mark.db
def test_state_is_loaded_only_for_the_batch(fresh_db: None) -> None:
    """M3-FR-11. Reading the whole table every ten minutes to score a few dozen
    events would get worse every day the project runs."""
    with connect() as conn:
        st: dict[str, Any] = {}
        for i, user in enumerate(["Alice", "Bob", "Carol"]):
            state.observe(
                st,
                {
                    "revid": i + 1,
                    "event_ts": NOW,
                    "title": f"Page{i}",
                    "user_name": user,
                    "user_id": 100 + i,
                    "is_reverting": False,
                },
            )
        state.persist(conn, st)

        batch = [{"revid": 9, "event_ts": NOW, "title": "Page0", "user_name": "Alice"}]
        loaded = state.load_for(conn, batch)

    assert set(loaded["editors"]) == {"Alice"}
    assert set(loaded["pages"]) == {"Page0"}
    assert loaded["editors"]["Alice"]["edits"] == 1


@pytest.mark.db
def test_the_frontier_survives_a_round_trip_and_never_retreats(fresh_db: None) -> None:
    with connect() as conn:
        st: dict[str, Any] = {}
        state.observe(
            st,
            {
                "revid": 1,
                "event_ts": NOW,
                "title": "P",
                "user_name": "A",
                "user_id": 9_000,
                "is_reverting": False,
            },
        )
        state.persist(conn, st)
        assert (
            state.load_for(conn, [{"revid": 2, "event_ts": NOW, "title": "P", "user_name": "A"}])[
                "max_user_id"
            ]
            == 9_000
        )

        # A later batch of older accounts must not drag the frontier back.
        lower: dict[str, Any] = {"max_user_id": 5}
        state.persist(conn, lower)
        assert (
            state.load_for(conn, [{"revid": 3, "event_ts": NOW, "title": "P", "user_name": "A"}])[
                "max_user_id"
            ]
            == 9_000
        )


@pytest.mark.db
def test_scoring_writes_a_prediction_and_is_idempotent(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3-FR-4 and FR-8. Re-running the scorer must add nothing, by constraint
    rather than by the caller remembering to check."""
    from bellwether import score

    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    path = tmp_path / "champion-test.pkl"
    path.write_bytes(pickle.dumps(ConstantModel(0.7), protocol=5))
    digest = registry.artifact_sha256(path)

    with connect() as conn:
        for revid in range(1, 6):
            _event(conn, revid, minutes_ago=30 + revid)
        _register(conn, "champion-test", digest)

    first = score.run(limit=100)
    second = score.run(limit=100)

    assert first["scored"] == 5
    assert second["scored"] == 0

    with connect() as conn:
        rows = conn.execute(
            "SELECT score, feature_hash, outcome_observable_at_scoring "
            "FROM register.predictions ORDER BY revid"
        ).fetchall()

    assert len(rows) == 5
    assert all(r["score"] == pytest.approx(0.7) for r in rows)
    assert all(len(r["feature_hash"]) == 64 for r in rows)
    assert not any(r["outcome_observable_at_scoring"] for r in rows)


@pytest.mark.db
def test_a_score_written_after_the_outcome_was_visible_is_flagged(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3-FR-10, the failure nobody would notice.

    If the scorer falls behind — and GitHub's cron ran roughly hourly against a
    nominal ten minutes — it will eventually score an edit that has already
    been reverted. Nothing raises. The score is simply trivially correct and
    accuracy improves for the worst possible reason.
    """
    from bellwether import score

    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    path = tmp_path / "champion-late.pkl"
    path.write_bytes(pickle.dumps(ConstantModel(0.5), protocol=5))
    digest = registry.artifact_sha256(path)

    with connect() as conn:
        _event(conn, 1, minutes_ago=180)
        conn.execute(
            """
            INSERT INTO outcome.revert_events
                (revert_revid, reverted_revid, revert_ts, method)
            VALUES (99, 1, now() - make_interval(mins => 60), 'mw-undo')
            """
        )
        _register(conn, "champion-late", digest)

    result = score.run(limit=100)
    assert result["late"] == 1

    with connect() as conn:
        row = conn.execute(
            "SELECT outcome_observable_at_scoring FROM register.predictions WHERE revid = 1"
        ).fetchone()
    assert row is not None and row["outcome_observable_at_scoring"] is True


@pytest.mark.db
def test_an_edit_whose_label_we_already_held_is_flagged(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3-FR-10, the limb the guard was missing.

    revert_events is built from reverting edits parsed out of edit summaries.
    Most outcomes never take that path — they arrive as mw-reverted tags, land
    in outcome.labels, and produce no revert_events row at all. An edit whose
    answer was already sitting in the database was therefore scored, and
    recorded, as though it had been predicted.
    """
    from bellwether import score

    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    path = tmp_path / "champion-known.pkl"
    path.write_bytes(pickle.dumps(ConstantModel(0.5), protocol=5))
    digest = registry.artifact_sha256(path)

    with connect() as conn:
        _event(conn, 1, minutes_ago=240)
        conn.execute(
            """
            INSERT INTO outcome.labels
                (revid, label, label_source, first_observed_at_utc,
                 detection_latency_seconds)
            VALUES (1, true, 'mw_reverted', now() - make_interval(mins => 90), 900)
            """
        )
        _register(conn, "champion-known", digest)

    assert score.run(limit=100)["late"] == 1

    with connect() as conn:
        row = conn.execute(
            "SELECT outcome_observable_at_scoring FROM register.predictions WHERE revid = 1"
        ).fetchone()
    assert row is not None and row["outcome_observable_at_scoring"] is True


@pytest.mark.db
def test_a_label_observed_after_scoring_is_a_real_prediction(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction, which matters just as much: the guard must not
    condemn an honest prediction simply because the outcome exists by the time
    anyone reads the register."""
    from bellwether import score

    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    path = tmp_path / "champion-honest.pkl"
    path.write_bytes(pickle.dumps(ConstantModel(0.5), protocol=5))
    digest = registry.artifact_sha256(path)

    with connect() as conn:
        _event(conn, 1, minutes_ago=10)
        conn.execute(
            """
            INSERT INTO outcome.labels
                (revid, label, label_source, first_observed_at_utc,
                 detection_latency_seconds)
            VALUES (1, true, 'mw_reverted', now() + make_interval(mins => 30), 900)
            """
        )
        _register(conn, "champion-honest", digest)

    assert score.run(limit=100)["late"] == 0


@pytest.mark.db
def test_edits_the_champion_was_trained_on_are_never_scored(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The register measures out-of-sample behaviour or it measures nothing.

    The lookback window and the training window are set independently, and
    where they overlap the scorer would file predictions on edits the model has
    memorised. This champion scores 0.686 in-sample against 0.256 out-of-sample,
    so the contamination would not be subtle.
    """
    from bellwether import score

    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    path = tmp_path / "champion-window.pkl"
    path.write_bytes(pickle.dumps(ConstantModel(0.5), protocol=5))
    digest = registry.artifact_sha256(path)

    with connect() as conn:
        # _register trains on [NOW - 2 days, NOW - 1 day). In minutes-ago terms
        # that is everything between 2,880 and 1,440 minutes back.
        _event(conn, 1, minutes_ago=2_000)  # inside the training window
        _event(conn, 2, minutes_ago=1_000)  # after it
        _register(conn, "champion-window", digest)
        conn.execute(
            "UPDATE register.model_registry SET training_start = now() - interval '2 days', "
            "training_end = now() - interval '1 day' WHERE model_version = 'champion-window'"
        )

    assert score.run(limit=100, lookback_days=7)["scored"] == 1

    with connect() as conn:
        rows = conn.execute("SELECT revid FROM register.predictions").fetchall()
    assert [r["revid"] for r in rows] == [2]


@pytest.mark.db
def test_the_scorer_refuses_without_a_registered_model(fresh_db: None) -> None:
    from bellwether import score

    with connect() as conn:
        _event(conn, 1)
    assert score.run(limit=10)["skipped"] is True


@pytest.mark.db
def test_the_scorer_refuses_a_mismatched_artifact(
    fresh_db: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry says one thing and the disk says another. Refuse, loudly,
    before a single score is written."""
    from bellwether import score

    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    (tmp_path / "champion-bad.pkl").write_bytes(pickle.dumps({"not": "the model"}, protocol=5))

    with connect() as conn:
        _event(conn, 1)
        _register(conn, "champion-bad", "f" * 64)

    with pytest.raises(registry.ArtifactMismatch):
        score.run(limit=10)

    with connect() as conn:
        n = conn.execute("SELECT count(*) AS n FROM register.predictions").fetchone()
    assert n is not None and n["n"] == 0
