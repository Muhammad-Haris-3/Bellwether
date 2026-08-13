"""Ingestion behaviour against a real Postgres.

Marked `db`. These are the tests that matter for M0-T2's acceptance criterion:
re-running an overlapping window must insert zero duplicates, and the cursor
must survive a run that dies part way through.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bellwether.db import connect
from bellwether.ingest import insert_page, read_cursor, write_cursor
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


def test_the_cursor_never_moves_backwards(fresh_db: None) -> None:
    """A backfill must not rewind live ingestion.

    `--since` sets its own window, so without monotonicity a backfill of a
    three-day-old window would drag the cursor back to where that window ended,
    and the next scheduled run would re-read everything from there to now — and
    would do it again after every backfill.

    The same guard covers a delayed run finishing after a later one and
    rewinding the cursor to its own older position.
    """
    run_id = new_run_id()
    live = datetime(2026, 8, 13, 12, tzinfo=UTC)
    backfill_end = datetime(2026, 8, 10, 14, tzinfo=UTC)

    with connect() as conn:
        write_cursor(conn, live, run_id)
        write_cursor(conn, backfill_end, run_id)

    with connect() as conn:
        assert read_cursor(conn) == live


def test_the_cursor_still_advances_forwards(fresh_db: None) -> None:
    run_id = new_run_id()
    with connect() as conn:
        write_cursor(conn, datetime(2026, 8, 13, 12, tzinfo=UTC), run_id)
        write_cursor(conn, datetime(2026, 8, 13, 13, tzinfo=UTC), run_id)

    with connect() as conn:
        assert read_cursor(conn) == datetime(2026, 8, 13, 13, tzinfo=UTC)


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


def test_stratum_and_weight_are_recorded_per_event(fresh_db: None) -> None:
    """M1-FR-2. The weight is the inverse sampling probability, written at
    observation time so population estimates survive a change of frame."""
    from bellwether import frame

    run_id = new_run_id()
    with connect() as conn:
        insert_page(conn, [_event(1, temp=True), _event(2, temp=False)], run_id)
        rows = conn.execute(
            "SELECT revid, sampling_stratum, sampling_weight, in_maturity_cohort "
            "FROM landing.rc_events ORDER BY revid"
        ).fetchall()

    assert [r["sampling_stratum"] for r in rows] == ["logged_out", "registered"]
    assert float(rows[0]["sampling_weight"]) == pytest.approx(
        100 / frame.SAMPLE_PERCENT["logged_out"]
    )
    assert float(rows[1]["sampling_weight"]) == pytest.approx(
        100 / frame.SAMPLE_PERCENT["registered"]
    )


def test_tags_are_stored_as_ids_against_the_dimension(fresh_db: None) -> None:
    """M1-FR-4. 67 distinct tags exist across the whole feed; storing the text
    on every row cost ~40 bytes of a 372-byte budget."""
    run_id = new_run_id()
    tagged = {**_event(1), "tags": ["mobile edit", "mw-reverted"]}
    with connect() as conn:
        insert_page(conn, [tagged], run_id)
        row = conn.execute("SELECT tag_ids FROM landing.rc_events WHERE revid = 1").fetchone()
        names = conn.execute("SELECT tag_name FROM landing.tag_names ORDER BY tag_name").fetchall()

    assert row is not None and len(row["tag_ids"]) == 2
    assert [n["tag_name"] for n in names] == ["mobile edit", "mw-reverted"]


def test_the_tag_dimension_does_not_duplicate_across_pages(fresh_db: None) -> None:
    run_id = new_run_id()
    cache: dict[str, int] = {}
    with connect() as conn:
        insert_page(conn, [{**_event(1), "tags": ["visualeditor"]}], run_id, cache)
    with connect() as conn:
        insert_page(conn, [{**_event(2), "tags": ["visualeditor"]}], run_id, cache)
        rows = conn.execute("SELECT count(*) AS n FROM landing.tag_names").fetchone()

    assert rows is not None and rows["n"] == 1
