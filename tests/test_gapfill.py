"""Gap detection and the decision to stop trying.

The interesting behaviours are the refusals: not healing a gap the source can
no longer serve, and not moving the cursor to close one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bellwether.db import connect
from bellwether.gapfill import Gap, find_gaps, is_permanent
from bellwether.runlog import utcnow

pytestmark = pytest.mark.db

THRESHOLD = 600


def _event(conn: Any, revid: int, minutes_ago: float) -> None:
    conn.execute(
        """
        INSERT INTO landing.rc_events
            (revid, event_ts, ns, title, is_anon, is_temp, is_minor, is_bot,
             sampling_stratum, sampling_weight, ingested_at_utc)
        VALUES (%s, now() - make_interval(mins => %s), 0, 'Page',
                false, false, false, false, 'registered', 2.0, now())
        """,
        (revid, minutes_ago),
    )


def _gap(minutes: float, *, attempts: int = 0, recovered: int = 0, age_days: int = 0) -> Gap:
    end = utcnow() - timedelta(days=age_days)
    return Gap(
        gap_from=end - timedelta(minutes=minutes),
        gap_to=end,
        seconds=int(minutes * 60),
        attempts=attempts,
        rows_recovered=recovered,
    )


def test_a_dense_run_of_events_has_no_gaps(fresh_db: None) -> None:
    with connect() as conn:
        for i in range(20):
            _event(conn, i + 1, minutes_ago=100 - i)
        assert find_gaps(conn, retention_days=30, threshold_seconds=THRESHOLD) == []


def test_a_silence_longer_than_the_threshold_is_a_gap(fresh_db: None) -> None:
    with connect() as conn:
        _event(conn, 1, minutes_ago=200)
        _event(conn, 2, minutes_ago=100)  # 100-minute hole
        gaps = find_gaps(conn, retention_days=30, threshold_seconds=THRESHOLD)

    assert len(gaps) == 1
    assert gaps[0].minutes == pytest.approx(100, abs=0.1)


def test_a_silence_under_the_threshold_is_not_a_gap(fresh_db: None) -> None:
    """Sampling thins the stream; it does not make holes.

    At the M1 frame the pipeline keeps ~6.4 events a minute, so a few minutes
    between kept events is ordinary. Ten minutes is not.
    """
    with connect() as conn:
        _event(conn, 1, minutes_ago=100)
        _event(conn, 2, minutes_ago=91)  # 9 minutes
        assert find_gaps(conn, retention_days=30, threshold_seconds=THRESHOLD) == []


def test_gaps_come_back_oldest_first(fresh_db: None) -> None:
    """M1-FR-12. Oldest first, because the old ones are the ones about to age
    past the point where the source can still serve them."""
    with connect() as conn:
        for revid, minutes in enumerate([600, 500, 300, 200, 60], start=1):
            _event(conn, revid, minutes_ago=minutes)
        gaps = find_gaps(conn, retention_days=30, threshold_seconds=THRESHOLD)

    assert [g.gap_from for g in gaps] == sorted(g.gap_from for g in gaps)


def test_gaps_outside_the_retention_window_are_not_reported(fresh_db: None) -> None:
    """Chasing a hole in data that is about to be deleted anyway is work for
    nothing."""
    with connect() as conn:
        _event(conn, 1, minutes_ago=60 * 24 * 40)  # 40 days
        _event(conn, 2, minutes_ago=60 * 24 * 39)
        _event(conn, 3, minutes_ago=10)
        gaps = find_gaps(conn, retention_days=30, threshold_seconds=THRESHOLD)

    assert all(g.gap_from > datetime.now(UTC) - timedelta(days=30) for g in gaps)


def test_a_gap_older_than_the_source_horizon_is_permanent() -> None:
    """The data is gone from recentchanges. No number of retries recovers it,
    and pretending otherwise burns a free API budget on a loop."""
    assert is_permanent(_gap(60, age_days=40), max_attempts=3, horizon_days=25)


def test_a_gap_that_keeps_yielding_nothing_becomes_permanent() -> None:
    assert is_permanent(_gap(60, attempts=3, recovered=0), max_attempts=3, horizon_days=25)


def test_a_gap_that_yielded_rows_stays_open() -> None:
    """Partial progress is progress. A window that gave up rows on earlier
    attempts may still have more."""
    assert not is_permanent(_gap(60, attempts=9, recovered=12), max_attempts=3, horizon_days=25)


def test_a_fresh_gap_is_never_permanent() -> None:
    assert not is_permanent(_gap(60), max_attempts=3, horizon_days=25)


def test_attempts_are_matched_by_containment(fresh_db: None) -> None:
    """A gap's edges move as it partially fills, so attempt history is matched
    by a window that contains the gap rather than by exact boundaries."""
    with connect() as conn:
        _event(conn, 1, minutes_ago=200)
        _event(conn, 2, minutes_ago=100)
        conn.execute(
            """
            INSERT INTO landing.gap_attempts (gap_from_utc, gap_to_utc, rows_added)
            VALUES (now() - make_interval(mins => 250), now() - make_interval(mins => 50), 0)
            """
        )
        gaps = find_gaps(conn, retention_days=30, threshold_seconds=THRESHOLD)

    assert gaps[0].attempts == 1
    assert gaps[0].rows_recovered == 0
