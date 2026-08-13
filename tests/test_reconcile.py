"""The state repair job (M3-FR-12, M3-FR-13).

The interesting tests here are the last two. The first few check that the job
can tell agreement from disagreement; the last two check that it catches the
thing it was built to catch, and that it refuses to paper over it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bellwether import reconcile, state
from bellwether.db import connect


def _event(
    conn: Any,
    revid: int,
    *,
    minutes_ago: int,
    user: str = "Alice",
    title: str = "Page",
    tags: str = "{}",
) -> None:
    conn.execute(
        """
        INSERT INTO landing.rc_events
            (revid, event_ts, ns, title, user_name, user_id, is_anon, is_temp,
             is_minor, is_bot, oldlen, newlen, tags, sampling_stratum,
             sampling_weight, ingested_at_utc)
        VALUES (%s, now() - make_interval(mins => %s), 0, %s, %s, 500, false,
                false, false, false, 100, 120, %s, 'registered', 33.3, now())
        """,
        (revid, minutes_ago, title, user, tags),
    )


def _replay_and_persist(days: int = 7) -> None:
    """Bring persisted state to exactly what a replay produces."""
    with connect() as conn:
        state.persist(conn, state.replay(conn, days=days)["state"])


@pytest.mark.db
def test_a_faithful_replay_reconciles(fresh_db: None) -> None:
    with connect() as conn:
        for revid in range(1, 6):
            _event(conn, revid, minutes_ago=100 - revid)
    _replay_and_persist()

    result = reconcile.run(days=7)
    assert result["divergences"] == 0
    assert result["agreement"] == 1.0
    assert result["checked"] > 0


@pytest.mark.db
def test_a_wrong_counter_fails_loudly_and_is_not_corrected(fresh_db: None) -> None:
    """M3-FR-13. The refusal to self-heal is the requirement, not a limitation.

    A job that quietly fixes drift removes the only signal that something is
    producing it, and the next morning everything looks fine again.
    """
    with connect() as conn:
        for revid in range(1, 4):
            _event(conn, revid, minutes_ago=100 - revid)
    _replay_and_persist()

    with connect() as conn:
        conn.execute("UPDATE landing.editor_state SET edits_seen = 99 WHERE user_key = 'Alice'")

    with pytest.raises(reconcile.StateDivergence, match="Not repairing"):
        reconcile.run(days=7)

    with connect() as conn:
        row = conn.execute(
            "SELECT edits_seen FROM landing.editor_state WHERE user_key = 'Alice'"
        ).fetchone()
    assert row is not None and row["edits_seen"] == 99, "the job must not have healed it"


@pytest.mark.db
def test_repair_is_available_but_only_when_asked(fresh_db: None) -> None:
    with connect() as conn:
        for revid in range(1, 4):
            _event(conn, revid, minutes_ago=100 - revid)
    _replay_and_persist()

    with connect() as conn:
        conn.execute("UPDATE landing.editor_state SET edits_seen = 99 WHERE user_key = 'Alice'")

    assert reconcile.run(days=7, repair=True)["repaired"] is True

    with connect() as conn:
        row = conn.execute(
            "SELECT edits_seen FROM landing.editor_state WHERE user_key = 'Alice'"
        ).fetchone()
    assert row is not None and row["edits_seen"] == 3

    # And it now agrees, which is what makes the repair verifiable.
    assert reconcile.run(days=7)["divergences"] == 0


@pytest.mark.db
def test_keys_older_than_the_window_are_out_of_scope(fresh_db: None) -> None:
    """A replay over N days cannot reproduce a counter built over more than N,
    and judging it anyway would make the agreement rate a function of how far
    back the job happens to look rather than of whether anything is wrong."""
    now = datetime.now(UTC)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO landing.editor_state
                (user_key, first_seen_utc, last_seen_utc, edits_seen,
                 reverts_performed, edits_reverted)
            VALUES ('Ancient', %s, %s, 4321, 0, 0)
            """,
            (now - timedelta(days=40), now - timedelta(days=39)),
        )
        _event(conn, 1, minutes_ago=60)
    _replay_and_persist()

    result = reconcile.run(days=7)
    assert result["divergences"] == 0, "the 40-day-old editor must not be judged"


@pytest.mark.db
def test_the_online_path_cannot_maintain_the_revert_counters(fresh_db: None) -> None:
    """The defect this job exists to surface.

    `state.observe` takes `was_reverted`, and the batch replay supplies it at
    the moment the revert happened. The online scorer calls the same function
    and never supplies it, because at scoring time the outcome does not exist
    yet — that is the entire premise of the project. Nothing revisits the edit
    when the revert arrives later.

    So `editor_edits_reverted` and `page_edits_reverted` are fed to the model as
    zero in production while training saw real values. One shared function was
    supposed to make train/serve skew impossible; it makes the FUNCTION
    identical, and the data reaching it is not.
    """
    with connect() as conn:
        _event(conn, 1, minutes_ago=200)
        _event(conn, 2, minutes_ago=100, user="Bob")
        conn.execute(
            """
            INSERT INTO outcome.revert_events (revert_revid, reverted_revid, revert_ts, method)
            VALUES (2, 1, now() - make_interval(mins => 150), 'mw-undo')
            """
        )

        # Exactly what the scorer does: load, fold, persist. No was_reverted,
        # because when an edit is scored nobody knows yet.
        batch = [
            {"revid": 1, "event_ts": datetime.now(UTC), "title": "Page", "user_name": "Alice"},
            {"revid": 2, "event_ts": datetime.now(UTC), "title": "Page", "user_name": "Bob"},
        ]
        st = state.load_for(conn, batch)
        for event in batch:
            state.observe(st, event)
        state.persist(conn, st)

    with pytest.raises(reconcile.StateDivergence) as caught:
        reconcile.run(days=7)

    message = str(caught.value)
    assert "divergences" in message

    # And it is specifically the revert counters that disagree.
    with connect() as conn:
        replayed = state.replay(conn, days=7)["state"]
    assert replayed["editors"]["Alice"]["edits_reverted"] == 1
    with connect() as conn:
        row = conn.execute(
            "SELECT edits_reverted FROM landing.editor_state WHERE user_key = 'Alice'"
        ).fetchone()
    assert row is not None and row["edits_reverted"] == 0
