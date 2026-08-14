"""The watchdog (M8 §6).

Two properties matter and they pull against each other: it must fire when the
pipeline has stopped, and it must stay silent on a system that has simply never
run. An alert that cannot tell those apart gets muted in a week, and then it is
worse than nothing because everyone believes it is watching.
"""

from __future__ import annotations

from typing import Any

import pytest

from bellwether import watchdog
from bellwether.db import connect


def _ran(conn: Any, job: str, *, minutes_ago: int) -> None:
    conn.execute(
        "INSERT INTO landing.run_log (run_id, job, started_at_utc, status) "
        "VALUES (gen_random_uuid(), %s, now() - make_interval(mins => %s), 'success')",
        (job, minutes_ago),
    )


@pytest.mark.db
def test_a_system_that_has_never_run_is_not_a_fault(fresh_db: None) -> None:
    """M8-FR-21. A deployment an hour old has not run its daily jobs, and
    alerting on that would make the watchdog noise from the moment it was
    switched on."""
    with connect() as conn:
        assert watchdog.check(conn) == []


@pytest.mark.db
def test_a_stopped_job_is_a_fault(fresh_db: None) -> None:
    with connect() as conn:
        _ran(conn, "ingest", minutes_ago=5)
        _ran(conn, "label", minutes_ago=600)
        faults = watchdog.check(conn)

    assert len(faults) == 1
    assert "label" in faults[0]
    assert "600 minutes ago" in faults[0]


@pytest.mark.db
def test_a_job_within_its_limit_is_silent(fresh_db: None) -> None:
    """Generous against the schedule: GitHub's cron is best-effort and a single
    skipped slot is normal. An alert must mean a pattern, not a hiccup."""
    with connect() as conn:
        for job, limit in watchdog.SILENCE_MINUTES.items():
            _ran(conn, job, minutes_ago=limit - 1)
        assert watchdog.check(conn) == []


@pytest.mark.db
def test_an_unreproducible_prediction_is_a_fault(fresh_db: None) -> None:
    """M3 promised the scorer's state is reconstructible, in public. A rate
    below 100% means that claim has stopped being true."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO register.reproductions "
            "(window_start, window_end, sampled, hash_matched, score_matched, "
            " matched_at_scoring_time, unreproducible) "
            "VALUES (now() - interval '2 days', now(), 100, 97, 97, 0, 3)"
        )
        faults = watchdog.check(conn)

    assert any("could not be reproduced" in fault for fault in faults)


@pytest.mark.db
def test_a_stale_deployment_is_a_fault(fresh_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """M8-FR-18, and the gap that hid three failed deploys.

    The container crashed on import, Render kept the last working one alive, and
    /health reported ok the whole time — because the OLD code was fine and
    answering.
    """
    from bellwether.config import get_settings

    # The setting is `commit`, surfaced through build_id, which also reads
    # the hosting platforms' own variables.
    monkeypatch.setenv("BELLWETHER_COMMIT", "b" * 40)
    get_settings.cache_clear()

    with connect() as conn:
        faults = watchdog.check(conn, deployed_build="a" * 40)
    assert any("deploy has failed silently" in fault for fault in faults)

    with connect() as conn:
        matching = watchdog.check(conn, deployed_build="b" * 40)
    assert matching == [], "a build that matches the repository is not a fault"


@pytest.mark.db
def test_it_raises_rather_than_returning_quietly(fresh_db: None) -> None:
    """The alert IS the failing run. Returning a list nobody reads would make
    the workflow green while the pipeline was stopped."""
    with connect() as conn:
        _ran(conn, "ingest", minutes_ago=9_999)

    with pytest.raises(watchdog.Stalled):
        watchdog.run()


@pytest.mark.db
def test_a_healthy_system_does_not_raise(fresh_db: None) -> None:
    with connect() as conn:
        _ran(conn, "ingest", minutes_ago=1)
    assert watchdog.run()["faults"] == []
