"""The watchdog (M8 §6).

Three properties matter and they pull against each other: it must fire when the
pipeline has stopped, it must stay silent on a system that has simply never run,
and it must not still be firing about the same thing a hundred runs later. An
alert that fails any of these gets muted in a week, and then it is worse than
nothing because everyone believes it is watching.
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


def _messages(faults: list[watchdog.Fault]) -> str:
    return " | ".join(fault.message for fault in faults)


def _unreproducible(conn: Any, n: int = 3) -> None:
    conn.execute(
        "INSERT INTO register.reproductions "
        "(window_start, window_end, sampled, hash_matched, score_matched, "
        " matched_at_scoring_time, unreproducible) "
        "VALUES (now() - interval '2 days', now(), 100, 97, 97, 0, %s)",
        (n,),
    )


def _age_fault(conn: Any, key: str, *, hours: int) -> None:
    """Backdate a remembered fault, so a test can reach the re-notify edge
    without waiting a day for it."""
    conn.execute(
        "UPDATE landing.watchdog_faults "
        "   SET first_seen   = now() - make_interval(hours => %(h)s), "
        "       last_alerted = now() - make_interval(hours => %(h)s) "
        " WHERE fault_key = %(key)s",
        {"h": hours, "key": key},
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
    assert faults[0].key == "silence:label"
    assert "600 minutes ago" in faults[0].message


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
        _unreproducible(conn)
        faults = watchdog.check(conn)

    assert "could not be reproduced" in _messages(faults)


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
    assert "deploy has failed silently" in _messages(faults)

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


# --- standing faults --------------------------------------------------------
#
# The reproduction rate fell below 100% on the 14th and the watchdog then failed
# one hundred consecutive runs. While it was red, a seven-hour ingest outage came
# and went underneath it, which is the exact failure it exists to catch.


@pytest.mark.db
def test_a_fault_alerts_once_and_then_stops_shouting(fresh_db: None) -> None:
    with connect() as conn:
        _unreproducible(conn)

    with pytest.raises(watchdog.Stalled):
        watchdog.run()

    # Unchanged, so the second run is green — and still says so out loud.
    result = watchdog.run()
    assert result["faults"] == []
    assert any("could not be reproduced" in message for message in result["standing"])


@pytest.mark.db
def test_a_new_fault_is_still_loud_while_another_one_stands(fresh_db: None) -> None:
    """The whole point. A standing fault that suppressed everything beside it
    would be the old always-red behaviour wearing a different colour."""
    with connect() as conn:
        _unreproducible(conn)
    with pytest.raises(watchdog.Stalled):
        watchdog.run()
    assert watchdog.run()["faults"] == [], "the reproduction fault has gone quiet"

    with connect() as conn:
        _ran(conn, "ingest", minutes_ago=9_999)

    with pytest.raises(watchdog.Stalled) as raised:
        watchdog.run()
    assert "ingest" in str(raised.value)
    assert "reproduced" not in str(raised.value), "the standing fault does not pad the alert"


@pytest.mark.db
def test_a_standing_fault_is_raised_again_after_a_day(fresh_db: None) -> None:
    """Quiet is not the same as tolerated. A condition nobody is ever made to
    look at again becomes the furniture."""
    with connect() as conn:
        _unreproducible(conn)
    with pytest.raises(watchdog.Stalled):
        watchdog.run()
    assert watchdog.run()["faults"] == []

    with connect() as conn:
        _age_fault(conn, "reproducibility", hours=watchdog.RENOTIFY_HOURS + 1)

    with pytest.raises(watchdog.Stalled):
        watchdog.run()


@pytest.mark.db
def test_a_fault_that_comes_back_reads_as_new(fresh_db: None) -> None:
    """To whoever has to act on it, a fault that cleared and returned IS new.
    The history of what actually happened is in run_log, which is append-only."""
    with connect() as conn:
        _ran(conn, "ingest", minutes_ago=9_999)
    with pytest.raises(watchdog.Stalled):
        watchdog.run()

    with connect() as conn:
        _ran(conn, "ingest", minutes_ago=1)
    result = watchdog.run()
    assert result["resolved"] == ["silence:ingest"]

    with connect() as conn:
        conn.execute("TRUNCATE landing.run_log")
        _ran(conn, "ingest", minutes_ago=9_999)
    with pytest.raises(watchdog.Stalled):
        watchdog.run()


@pytest.mark.db
def test_the_wording_may_change_without_becoming_a_different_fault(fresh_db: None) -> None:
    """`ingest last ran 77 minutes ago` and `...83 minutes ago` are one
    continuing fault. Keying on the message would make every single run a fresh
    alert, which is the always-red behaviour rebuilt by accident."""
    with connect() as conn:
        _ran(conn, "ingest", minutes_ago=9_999)
    with pytest.raises(watchdog.Stalled):
        watchdog.run()

    with connect() as conn:
        conn.execute("TRUNCATE landing.run_log")
        _ran(conn, "ingest", minutes_ago=12_345)

    result = watchdog.run()
    assert result["faults"] == []
    assert any("12345 minutes ago" in message for message in result["standing"]), (
        "the report carries the current number, not the one it opened with"
    )
