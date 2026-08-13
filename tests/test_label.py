"""Primary label path.

The behaviours worth protecting here are the ones whose failure would not raise
an error — it would just quietly move a published number.
"""

from __future__ import annotations

from typing import Any

import pytest

from bellwether.config import LABEL_CHECKPOINTS_SECONDS
from bellwether.db import connect
from bellwether.label import FINAL_CHECKPOINT, due_checks

pytestmark = pytest.mark.db


def _insert_event(conn: Any, revid: int, age_seconds: int, *, cohort: bool = True) -> None:
    """Seed an event. Defaults into the maturity cohort.

    Most of these tests are about the checkpoint grid, and only the cohort
    receives it (M1 §5). The default keeps those tests about what they are
    testing; `cohort=False` is used where the split itself is the subject.
    """
    conn.execute(
        """
        INSERT INTO landing.rc_events
            (revid, event_ts, ns, title, is_anon, is_temp, is_minor, is_bot,
             sampling_stratum, in_maturity_cohort, ingested_at_utc)
        VALUES (%s, now() - make_interval(secs => %s), 0, 'Page',
                false, false, false, false, 'registered', %s, now())
        """,
        (revid, age_seconds, cohort),
    )


def test_an_edit_is_due_only_at_checkpoints_it_has_reached(fresh_db: None) -> None:
    with connect() as conn:
        _insert_event(conn, 1, age_seconds=30)  # younger than the 1h checkpoint
        _insert_event(conn, 2, age_seconds=7_200)  # past 1h, short of 6h
        due = due_checks(conn, limit=100)

    by_revid: dict[int, list[int]] = {}
    for row in due:
        by_revid.setdefault(int(row["revid"]), []).append(int(row["checkpoint_seconds"]))

    assert 1 not in by_revid
    assert by_revid[2] == [3_600]


def test_a_recorded_check_is_not_repeated(fresh_db: None) -> None:
    with connect() as conn:
        _insert_event(conn, 1, age_seconds=7_200)
        conn.execute(
            """
            INSERT INTO outcome.label_checks
                (revid, checkpoint_seconds, checked_at_utc, age_seconds, had_reverted_tag)
            VALUES (1, 3600, now(), 3700, false)
            """
        )
        assert due_checks(conn, limit=100) == []


def test_a_known_revert_is_never_re_checked(fresh_db: None) -> None:
    """Once the outcome is known, asking again spends someone else's bandwidth
    to learn nothing."""
    with connect() as conn:
        _insert_event(conn, 1, age_seconds=200_000)  # past every checkpoint but the last
        conn.execute(
            """
            INSERT INTO outcome.labels
                (revid, label, label_source, first_observed_at_utc, detection_latency_seconds)
            VALUES (1, true, 'mw_reverted', now(), 3600)
            """
        )
        assert due_checks(conn, limit=100) == []


def test_least_overdue_checks_come_first(fresh_db: None) -> None:
    """A check nominally due at one hour but performed at five records an age
    of five. Honest, but it degrades the grid the survival estimate is built
    on, so the queue is ordered to keep the grid as intended."""
    with connect() as conn:
        _insert_event(conn, 1, age_seconds=500_000)  # 1h checkpoint is wildly overdue
        _insert_event(conn, 2, age_seconds=3_700)  # 1h checkpoint is barely due
        due = due_checks(conn, limit=100)

    first = due[0]
    assert int(first["revid"]) == 2
    assert int(first["checkpoint_seconds"]) == 3_600


def test_a_non_cohort_edit_is_checked_once_at_maturity(fresh_db: None) -> None:
    """M1-FR-7. Five rows an event was the largest storage line after
    rc_events itself, and the grid exists to estimate one curve in M2 — not to
    run on every event forever."""
    with connect() as conn:
        _insert_event(conn, 1, age_seconds=700_000, cohort=False)
        due = due_checks(conn, limit=100)

    assert [int(r["checkpoint_seconds"]) for r in due] == [FINAL_CHECKPOINT]


def test_a_cohort_edit_of_the_same_age_gets_the_whole_grid(fresh_db: None) -> None:
    with connect() as conn:
        _insert_event(conn, 1, age_seconds=700_000, cohort=True)
        due = due_checks(conn, limit=100)

    assert len(due) == len(LABEL_CHECKPOINTS_SECONDS)


def test_a_young_non_cohort_edit_is_not_due_at_all(fresh_db: None) -> None:
    """It has passed the 1h checkpoint, but that checkpoint is not its to reach."""
    with connect() as conn:
        _insert_event(conn, 1, age_seconds=7_200, cohort=False)
        assert due_checks(conn, limit=100) == []


def test_the_final_checkpoint_is_the_last_one_configured() -> None:
    """The point at which an unreverted edit is finally called negative.

    A placeholder for the maturity window that M2 estimates. Asserted so that
    adding a longer checkpoint moves it deliberately rather than silently.
    """
    assert FINAL_CHECKPOINT == 604_800


def test_limit_bounds_the_queue(fresh_db: None) -> None:
    """One run must fit inside the workflow budget (NFR-5)."""
    with connect() as conn:
        for revid in range(1, 11):
            _insert_event(conn, revid, age_seconds=700_000)  # due at all five checkpoints
        assert len(due_checks(conn, limit=7)) == 7
