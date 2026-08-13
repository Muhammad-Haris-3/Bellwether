"""The prediction register.

The only artefact in this project that cannot be rebuilt. Features, state, even
the raw events can be recomputed or re-fetched from the wiki. A score, once
lost or altered, is gone — and with it the thing that separates a prediction
from a description.

Every test here checks a property of the DATABASE, not of the code that writes
to it. A guarantee enforced by the caller behaving well is not a guarantee.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest

from bellwether.db import connect

pytestmark = pytest.mark.db

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


def _predict(conn: Any, revid: int = 1, **kw: Any) -> None:
    conn.execute(
        """
        INSERT INTO register.predictions
            (revid, event_ts, scored_at, model_version, role, score, feature_hash)
        VALUES (%(revid)s, %(event_ts)s, %(scored_at)s, %(model)s, %(role)s,
                %(score)s, %(hash)s)
        """,
        {
            "revid": revid,
            "event_ts": kw.get("event_ts", NOW),
            "scored_at": kw.get("scored_at", NOW + timedelta(minutes=12)),
            "model": kw.get("model", "v1"),
            "role": kw.get("role", "champion"),
            "score": kw.get("score", 0.42),
            "hash": kw.get("hash", "abc123"),
        },
    )


def test_a_score_cannot_be_backdated(fresh_db: None) -> None:
    """D-3. The simplest way to fake a forecasting record is to write the
    prediction after seeing the outcome and stamp it earlier."""
    with connect() as conn, pytest.raises(psycopg.errors.CheckViolation):
        _predict(conn, scored_at=NOW - timedelta(seconds=1))


def test_scoring_at_the_same_instant_is_allowed(fresh_db: None) -> None:
    """The constraint forbids going backwards, not acting promptly."""
    with connect() as conn:
        _predict(conn, scored_at=NOW)


def test_the_writer_cannot_change_a_score(fresh_db: None) -> None:
    """D-2, and the point of the whole milestone.

    Everything M0 built to guarantee the outcome was honestly observed is worth
    nothing if the prediction can be edited once the outcome is known.
    """
    with connect() as conn:
        _predict(conn)

    with connect() as conn:
        conn.execute("SET ROLE bellwether_writer")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE register.predictions SET score = 0.99 WHERE revid = 1")


def test_the_writer_cannot_delete_a_score(fresh_db: None) -> None:
    with connect() as conn:
        _predict(conn)

    with connect() as conn:
        conn.execute("SET ROLE bellwether_writer")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM register.predictions WHERE revid = 1")


def test_the_writer_cannot_truncate_the_register(fresh_db: None) -> None:
    with connect() as conn:
        conn.execute("SET ROLE bellwether_writer")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("TRUNCATE register.predictions")


def test_the_readonly_role_cannot_write_a_score(fresh_db: None) -> None:
    """The serving API must not be able to add to the record it reports."""
    with connect() as conn:
        conn.execute("SET ROLE bellwether_readonly")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _predict(conn)


def test_the_writer_can_append(fresh_db: None) -> None:
    """Append-only must not be so broad it stops the scorer working."""
    with connect() as conn:
        conn.execute("SET ROLE bellwether_writer")
        _predict(conn, revid=7)


def test_one_score_per_model_and_role(fresh_db: None) -> None:
    """M3-FR-8. Re-running the scorer is idempotent by constraint, not by the
    caller remembering to check."""
    with connect() as conn:
        _predict(conn, revid=1, model="v1", role="champion")
        with pytest.raises(psycopg.errors.UniqueViolation):
            _predict(conn, revid=1, model="v1", role="champion", score=0.99)


def test_a_shadow_may_score_the_same_event(fresh_db: None) -> None:
    """M3-FR-7. The register carries `role` from the outset so M5 needs no
    migration on an evidential table — altering this one later is exactly what
    should be hard."""
    with connect() as conn:
        _predict(conn, revid=1, role="champion")
        _predict(conn, revid=1, role="shadow", score=0.61)
        n = conn.execute(
            "SELECT count(*) AS n FROM register.predictions WHERE revid = 1"
        ).fetchone()
    assert n is not None and n["n"] == 2


def test_a_new_model_version_may_rescore(fresh_db: None) -> None:
    with connect() as conn:
        _predict(conn, revid=1, model="v1")
        _predict(conn, revid=1, model="v2", score=0.5)


def test_an_unknown_role_is_rejected(fresh_db: None) -> None:
    with connect() as conn, pytest.raises(psycopg.errors.CheckViolation):
        _predict(conn, role="experimental")


def test_a_score_outside_zero_to_one_is_rejected(fresh_db: None) -> None:
    """A probability that is not one is a bug somewhere upstream, and it should
    stop at the register rather than reach a calibration curve."""
    with connect() as conn, pytest.raises(psycopg.errors.CheckViolation):
        _predict(conn, score=1.4)
    with connect() as conn, pytest.raises(psycopg.errors.CheckViolation):
        _predict(conn, score=-0.1)


def test_predictions_are_pruned_only_from_a_sealed_month(fresh_db: None) -> None:
    """M3-FR-20. Same rule as labels: forgetting to seal stops the pruning
    rather than destroying what the seal was meant to attest to."""
    old = datetime(2020, 5, 17, tzinfo=UTC)
    with connect() as conn:
        _predict(conn, revid=1, event_ts=old, scored_at=old + timedelta(minutes=5))
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rows_affected FROM landing.prune_expired(p_dry_run => false) "
                "WHERE target = 'register.predictions'"
            )
            unsealed = cur.fetchone()
        assert unsealed is not None and int(unsealed["rows_affected"]) == 0

        conn.execute(
            "INSERT INTO outcome.seals (month, row_counts, digest) "
            "VALUES ('2020-05-01', '{}'::jsonb, 'x')"
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rows_affected FROM landing.prune_expired(p_dry_run => false) "
                "WHERE target = 'register.predictions'"
            )
            sealed = cur.fetchone()
        assert sealed is not None and int(sealed["rows_affected"]) == 1


def test_predictions_are_sealed(fresh_db: None) -> None:
    """A prediction pruned without a seal leaves no proof it was ever made,
    which would undo the entire milestone."""
    from bellwether import seal

    assert "register.predictions" in seal.SEALED

    old = datetime(2020, 5, 17, tzinfo=UTC)
    with connect() as conn:
        _predict(conn, revid=1, event_ts=old, scored_at=old + timedelta(minutes=5))
        digest, counts = seal.compute_digest(conn, date(2020, 5, 1))

    assert counts["register.predictions"] == 1
    assert len(digest) == 64
