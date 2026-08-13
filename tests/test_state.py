"""Point-in-time state.

The property under test is an ordering, not a value: features are emitted
before the event is folded in. Everything else here follows from that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bellwether import features, state


def _event(revid: int, *, minutes: int, user: str = "Alice", page: str = "P") -> dict[str, Any]:
    return {
        "revid": revid,
        "event_ts": datetime(2026, 8, 14, 12, tzinfo=UTC) + timedelta(minutes=minutes),
        "title": page,
        "user_name": user,
        "user_id": 100,
        "is_reverting": False,
        "sampling_stratum": "registered",
        "reverted_at": None,
    }


def test_an_events_own_history_excludes_itself() -> None:
    """The ordering that makes everything else valid.

    Folding before emitting would let an editor's very first edit see one prior
    edit — their own. Every history feature would be off by one, in the
    direction that makes a new account look established.
    """
    st: dict[str, Any] = {}
    first = _event(1, minutes=0)

    history = state.history_for(st, first)
    assert history["editor_edits_seen"] == 0

    state.observe(st, first)
    assert state.history_for(st, _event(2, minutes=1))["editor_edits_seen"] == 1


def test_history_accumulates_in_order() -> None:
    st: dict[str, Any] = {}
    for i in range(5):
        event = _event(i + 1, minutes=i)
        assert state.history_for(st, event)["editor_edits_seen"] == i
        state.observe(st, event)


def test_a_different_editor_has_no_history() -> None:
    st: dict[str, Any] = {}
    for i in range(3):
        state.observe(st, _event(i + 1, minutes=i, user="Alice"))

    assert state.history_for(st, _event(9, minutes=9, user="Bob"))["editor_edits_seen"] == 0
    assert state.history_for(st, _event(9, minutes=9, user="Bob"))["page_edits_seen"] == 3


def test_a_revert_counts_from_when_it_happened_not_when_the_edit_was_made() -> None:
    """The distinction that keeps the counter honest.

    An edit made at noon and reverted at three has a clean history at one
    o'clock. Folding the outcome in at the time of the EDIT would make every
    read after noon aware of something that had not happened yet — leakage
    wearing the clothes of an aggregate.
    """
    st: dict[str, Any] = {}
    bad = _event(1, minutes=0)
    state.observe(st, bad)

    # An hour later, before the revert: nothing known.
    assert state.history_for(st, _event(2, minutes=60))["editor_edits_reverted"] == 0

    # The revert happens; only now is it visible.
    state.observe_revert(st, bad)
    assert state.history_for(st, _event(3, minutes=200))["editor_edits_reverted"] == 1


def test_reverts_performed_counts_this_editors_own_reverts() -> None:
    """Patrollers revert prolifically and are almost never reverted. This is
    the one editor signal observable for the whole feed, because revert_events
    is recorded outside the sampling frame."""
    st: dict[str, Any] = {}
    state.observe(st, {**_event(1, minutes=0), "is_reverting": True})
    state.observe(st, {**_event(2, minutes=1), "is_reverting": True})
    state.observe(st, _event(3, minutes=2))

    history = state.history_for(st, _event(4, minutes=3))
    assert history["editor_reverts_performed"] == 2
    assert history["editor_edits_seen"] == 3


def test_history_features_read_only_declared_keys() -> None:
    """A feature reaching into the state dict directly could see any activity,
    and the guard's future-activity probe would not catch it — the probe adds
    keys, it does not police attribute access."""
    st: dict[str, Any] = {}
    state.observe(st, _event(1, minutes=0))
    history = state.history_for(st, _event(2, minutes=1))

    vector = features.build(_event(2, minutes=1), history)
    assert vector["editor_edits_seen"] == 1.0
    assert vector["editor_is_new_to_us"] == 0.0


def test_a_first_time_editor_is_flagged_as_new() -> None:
    vector = features.build(_event(1, minutes=0), state.history_for({}, _event(1, minutes=0)))
    assert vector["editor_is_new_to_us"] == 1.0
    assert vector["editor_edits_seen"] == 0.0
    assert vector["editor_days_known"] == 0.0


def test_days_known_is_finite_without_a_first_seen() -> None:
    """A NaN here reaches the model as a dropped row or a crash at training
    time, discovered somewhere far from this file."""
    vector = features.build(_event(1, minutes=0), {"editor_first_seen": None})
    assert vector["editor_days_known"] == 0.0


def test_the_guard_still_passes_with_history_features_present() -> None:
    """History features were added after the guard. The guard covers them, so
    they could not be added without being checked — that was the point of
    building it first."""
    from bellwether import knowability

    knowability.run_all()


@pytest.mark.db
def test_replay_reports_coverage_per_stratum(fresh_db: None) -> None:
    """M2-FR-12. The frame keeps 3% of registered edits, so most registered
    editors appear with no prior history. Whether that makes the features
    useless is measured, not assumed."""
    from bellwether.db import connect

    with connect() as conn:
        for i in range(6):
            conn.execute(
                """
                INSERT INTO landing.rc_events
                    (revid, event_ts, ns, title, user_name, is_anon, is_temp, is_minor,
                     is_bot, sampling_stratum, sampling_weight, ingested_at_utc)
                VALUES (%s, now() - make_interval(mins => %s), 0, 'Page', %s,
                        false, false, false, false, 'registered', 33.3, now())
                """,
                (i + 1, 60 - i, "Alice" if i % 2 == 0 else f"User{i}"),
            )
        result = state.replay(conn, days=30)

    coverage = result["coverage"]["by_stratum"]["registered"]
    assert coverage["scored"] == 6
    # Alice edits at positions 0, 2, 4 — the second and third see prior history.
    assert coverage["with_history"] == 2
