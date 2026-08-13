"""The guarantees that must hold at the database layer, not in Python.

If these pass only because the code never tries the forbidden operation, they
prove nothing. Each one performs the operation and requires it to be refused.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from bellwether.db import connect
from bellwether.runlog import new_run_id

pytestmark = pytest.mark.db


def _seed_event(conn: psycopg.Connection, revid: int = 1) -> None:
    conn.execute(
        """
        INSERT INTO landing.rc_events
            (revid, event_ts, ns, title, is_anon, is_minor, is_bot,
             sampling_stratum, ingested_at_utc)
        VALUES (%s, %s, 0, 'Page', true, false, false, 'anon', %s)
        ON CONFLICT (revid) DO NOTHING
        """,
        (revid, datetime(2026, 8, 13, 10, tzinfo=UTC), datetime(2026, 8, 13, 10, 1, tzinfo=UTC)),
    )


def _seed_label(conn: psycopg.Connection, revid: int = 1) -> None:
    conn.execute(
        """
        INSERT INTO outcome.labels
            (revid, label, label_source, first_observed_at_utc, detection_latency_seconds)
        VALUES (%s, true, 'mw_reverted', %s, 3600)
        ON CONFLICT (revid, label_source) DO NOTHING
        """,
        (revid, datetime(2026, 8, 13, 11, tzinfo=UTC)),
    )


def test_writer_cannot_update_a_label(fresh_db: None) -> None:
    """FR-10 / M0 acceptance criterion A-5.

    A system that can rewrite what it knew, and when it knew it, can claim any
    accuracy it likes. This is the test that says it cannot.
    """
    with connect() as conn:
        _seed_event(conn)
        _seed_label(conn)

    with connect() as conn:
        conn.execute("SET ROLE bellwether_writer")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE outcome.labels SET label = false WHERE revid = 1")


def test_writer_cannot_delete_a_label(fresh_db: None) -> None:
    with connect() as conn:
        _seed_event(conn)
        _seed_label(conn)

    with connect() as conn:
        conn.execute("SET ROLE bellwether_writer")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM outcome.labels WHERE revid = 1")


def test_writer_cannot_delete_an_observed_event(fresh_db: None) -> None:
    with connect() as conn:
        _seed_event(conn)

    with connect() as conn:
        conn.execute("SET ROLE bellwether_writer")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM landing.rc_events WHERE revid = 1")


def test_writer_cannot_discard_a_negative_label_check(fresh_db: None) -> None:
    """The checks that found nothing are the data.

    A survival curve estimated only from the checks that found something is not
    a survival curve. Deleting the negatives would not raise an error anywhere
    downstream — it would just move the maturity window.
    """
    with connect() as conn:
        _seed_event(conn)
        conn.execute(
            """
            INSERT INTO outcome.label_checks
                (revid, checkpoint_seconds, checked_at_utc, age_seconds, had_reverted_tag)
            VALUES (1, 3600, %s, 3600, false)
            """,
            (datetime(2026, 8, 13, 11, tzinfo=UTC),),
        )

    with connect() as conn:
        conn.execute("SET ROLE bellwether_writer")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM outcome.label_checks WHERE revid = 1")


def test_writer_can_still_move_the_cursor(fresh_db: None) -> None:
    """The append-only rule must not be so broad it stops the pipeline working.

    cursors and run_log are working state, not evidence.
    """
    with connect() as conn:
        conn.execute("SET ROLE bellwether_writer")
        conn.execute(
            """
            INSERT INTO landing.cursors (job, position_utc) VALUES ('ingest', %s)
            ON CONFLICT (job) DO UPDATE SET position_utc = EXCLUDED.position_utc
            """,
            (datetime(2026, 8, 13, 12, tzinfo=UTC),),
        )


def test_readonly_cannot_insert_anything(fresh_db: None) -> None:
    with connect() as conn:
        conn.execute("SET ROLE bellwether_readonly")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _seed_event(conn)


def test_one_label_per_source_per_revision(fresh_db: None) -> None:
    """FR-10: a later re-observation must not overwrite the first."""
    with connect() as conn:
        _seed_event(conn)
        _seed_label(conn)
        conn.execute(
            """
            INSERT INTO outcome.labels
                (revid, label, label_source, first_observed_at_utc, detection_latency_seconds)
            VALUES (1, true, 'mw_reverted', %s, 999999)
            ON CONFLICT (revid, label_source) DO NOTHING
            """,
            (datetime(2026, 8, 14, tzinfo=UTC),),
        )
        row = conn.execute(
            "SELECT detection_latency_seconds AS d FROM outcome.labels WHERE revid = 1"
        ).fetchone()

    assert row is not None and row["d"] == 3600  # the first observation stands


def test_both_label_sources_can_coexist(fresh_db: None) -> None:
    """FR-11 needs the two paths recorded independently to be comparable."""
    with connect() as conn:
        _seed_event(conn)
        _seed_label(conn)
        conn.execute(
            """
            INSERT INTO outcome.labels
                (revid, label, label_source, first_observed_at_utc, detection_latency_seconds)
            VALUES (1, true, 'revert_tag', %s, 1800)
            """,
            (datetime(2026, 8, 13, 10, 30, tzinfo=UTC),),
        )
        row = conn.execute("SELECT count(*) AS n FROM outcome.labels WHERE revid = 1").fetchone()

    assert row is not None and row["n"] == 2


def test_cannot_claim_to_have_observed_an_edit_before_it_happened(fresh_db: None) -> None:
    """The five-minute tolerance is for NTP drift, not for backdating."""
    event_ts = datetime(2026, 8, 13, 10, tzinfo=UTC)
    with connect() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            """
            INSERT INTO landing.rc_events
                (revid, event_ts, ns, title, is_anon, is_minor, is_bot,
                 sampling_stratum, ingested_at_utc)
            VALUES (99, %s, 0, 'Page', true, false, false, 'anon', %s)
            """,
            (event_ts, event_ts - timedelta(hours=1)),
        )


def test_run_log_records_a_failed_run(fresh_db: None) -> None:
    """A log of successes only is silent exactly when it is needed."""
    from bellwether.runlog import RunContext

    run_id = new_run_id()
    with pytest.raises(ValueError, match="boom"), RunContext(run_id, job="test"):
        raise ValueError("boom")

    with connect() as conn:
        row = conn.execute(
            "SELECT status, error_class, error_detail FROM landing.run_log WHERE run_id = %s",
            (run_id,),
        ).fetchone()

    assert row is not None
    assert row["status"] == "failed"
    assert row["error_class"] == "ValueError"
    assert "boom" in row["error_detail"]
