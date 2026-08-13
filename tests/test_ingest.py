"""Ingestion behaviour against a real Postgres.

Marked `db`. These are the tests that matter for M0-T2's acceptance criterion:
re-running an overlapping window must insert zero duplicates, and the cursor
must survive a run that dies part way through.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bellwether.db import connect
from bellwether.ingest import insert_page, read_cursor, stratum_of, write_cursor
from bellwether.runlog import new_run_id

pytestmark = pytest.mark.db


def _event(revid: int, *, minutes: int = 0, temp: bool = False) -> dict:
    return {
        "revid": revid,
        "old_revid": revid - 1,
        "rcid": revid,
        "event_ts": datetime(2026, 8, 13, 10, tzinfo=UTC) + timedelta(minutes=minutes),
        "ns": 0,
        "title": f"Page {revid}",
        "user_name": "~2026-44334-20" if temp else "Editor",
        "user_id": 55204706 if temp else 99,
        "is_anon": False,
        "is_temp": temp,
        "is_minor": False,
        "is_bot": False,
        "comment": "c",
        "comment_hidden": False,
        "user_hidden": False,
        "oldlen": 100,
        "newlen": 120,
        "tags": [],
    }


def test_reingesting_the_same_window_inserts_no_duplicates(fresh_db: None) -> None:
    """M0 acceptance criterion A-3."""
    run_id = new_run_id()
    page = [_event(r, minutes=r) for r in range(1, 6)]

    with connect() as conn:
        first = insert_page(conn, page, run_id)
    with connect() as conn:
        second = insert_page(conn, page, run_id)

    assert first == 5
    assert second == 0

    with connect() as conn:
        total = conn.execute("SELECT count(*) AS n FROM landing.rc_events").fetchone()
    assert total is not None and total["n"] == 5


def test_overlapping_windows_insert_only_the_new_rows(fresh_db: None) -> None:
    """The cursor is inclusive, so every run re-reads its predecessor's last
    second. That overlap must cost nothing but a few suppressed inserts."""
    run_id = new_run_id()
    with connect() as conn:
        insert_page(conn, [_event(r, minutes=r) for r in range(1, 6)], run_id)
    with connect() as conn:
        added = insert_page(conn, [_event(r, minutes=r) for r in range(4, 9)], run_id)

    assert added == 3  # 6, 7, 8 — 4 and 5 already present

    with connect() as conn:
        total = conn.execute("SELECT count(*) AS n FROM landing.rc_events").fetchone()
    assert total is not None and total["n"] == 8


def test_cursor_round_trips(fresh_db: None) -> None:
    run_id = new_run_id()
    position = datetime(2026, 8, 13, 11, 30, tzinfo=UTC)

    with connect() as conn:
        assert read_cursor(conn) is None
        write_cursor(conn, position, run_id)

    with connect() as conn:
        assert read_cursor(conn) == position


def test_cursor_does_not_advance_when_the_page_fails(fresh_db: None) -> None:
    """A run that dies between committing rows and moving the cursor must leave
    the cursor behind, not ahead. Behind costs a re-read; ahead loses data."""
    run_id = new_run_id()
    start = datetime(2026, 8, 13, 10, tzinfo=UTC)

    with connect() as conn:
        write_cursor(conn, start, run_id)

    with pytest.raises(RuntimeError), connect() as conn:  # noqa: PT012
        insert_page(conn, [_event(1, minutes=1)], run_id)
        raise RuntimeError("simulated crash before write_cursor")

    with connect() as conn:
        assert read_cursor(conn) == start


def test_stratum_is_recorded_per_event(fresh_db: None) -> None:
    run_id = new_run_id()
    with connect() as conn:
        insert_page(conn, [_event(1, temp=True), _event(2, temp=False)], run_id)
        rows = conn.execute(
            "SELECT revid, sampling_stratum, sampling_weight FROM landing.rc_events ORDER BY revid"
        ).fetchall()

    assert [r["sampling_stratum"] for r in rows] == ["logged_out", "registered"]
    assert all(float(r["sampling_weight"]) == 1.0 for r in rows)


def test_a_temporary_account_is_logged_out_not_registered() -> None:
    """The finding that reshaped the sampling frame (SRS 6.3).

    English Wikipedia masks IP addresses behind temporary accounts, so a
    logged-out editor arrives as a named account. Classifying by `anon` alone
    put 100% of a live 2,498-edit sample into the registered stratum — which
    would have sampled away most of the positive class before training began.
    """
    assert stratum_of({"is_anon": False, "is_temp": True}) == "logged_out"
    assert stratum_of({"is_anon": True, "is_temp": False}) == "logged_out"
    assert stratum_of({"is_anon": False, "is_temp": False}) == "registered"


def test_stratum_tolerates_an_event_without_the_temp_key() -> None:
    """Rows parsed before is_temp existed must not crash the classifier."""
    assert stratum_of({"is_anon": False}) == "registered"
