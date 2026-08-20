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


def _score_the_batch(conn: Any) -> None:
    """Exactly what the scorer does to state: load, fold in event order, persist.

    No `was_reverted`, because when an edit is scored nobody knows yet — that is
    the premise of the project, not an oversight in the scorer.
    """
    batch = conn.execute(
        "SELECT revid, event_ts, title, user_name, user_id FROM landing.rc_events "
        "ORDER BY event_ts, revid"
    ).fetchall()
    st = state.load_for(conn, batch)
    for event in batch:
        state.observe(st, event)
    state.persist(conn, st)


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
def test_a_repair_leaves_keys_the_comparison_would_not_judge_alone(fresh_db: None) -> None:
    """The repair has to be scoped exactly like the comparison, and for a
    stronger reason.

    An editor active for months has a stored counter built over months. A
    seven-day replay sees only the tail of it, which is why the comparison
    refuses to judge that editor at all. A repair that wrote the whole replayed
    state anyway would overwrite the lifetime count with the seven-day one —
    for exactly the editors carrying the most history, and silently, because
    the next run leaves them out of scope again and calls the table clean.
    """
    now = datetime.now(UTC)
    with connect() as conn:
        # Months of history the replay cannot see, and one recent edit that
        # puts this editor into the replay's result anyway.
        conn.execute(
            """
            INSERT INTO landing.editor_state
                (user_key, first_seen_utc, last_seen_utc, edits_seen,
                 reverts_performed, edits_reverted)
            VALUES ('Veteran', %s, %s, 4321, 0, 0)
            """,
            (now - timedelta(days=200), now - timedelta(days=1)),
        )
        _event(conn, 1, minutes_ago=60, user="Veteran", title="Veteran page")
        # And a genuine divergence on an in-scope key, so the repair runs.
        _event(conn, 2, minutes_ago=50, user="Newcomer", title="New page")
    _replay_and_persist()
    with connect() as conn:
        conn.execute("UPDATE landing.editor_state SET edits_seen = 99 WHERE user_key = 'Newcomer'")

    assert reconcile.run(days=7, repair=True)["repaired"] is True

    with connect() as conn:
        veteran = conn.execute(
            "SELECT edits_seen FROM landing.editor_state WHERE user_key = 'Veteran'"
        ).fetchone()
        newcomer = conn.execute(
            "SELECT edits_seen FROM landing.editor_state WHERE user_key = 'Newcomer'"
        ).fetchone()

    assert veteran is not None and veteran["edits_seen"] == 4321, (
        "the repair truncated a lifetime counter to the replay window"
    )
    assert newcomer is not None and newcomer["edits_seen"] == 1


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

        _score_the_batch(conn)

    with pytest.raises(reconcile.StateDivergence):
        reconcile.run(days=7)

    # Specifically the revert counters: the replay knows, the online path does
    # not, and nothing was ever going to tell it.
    with connect() as conn:
        replayed = state.replay(conn, days=7)["state"]
        stored = conn.execute(
            "SELECT edits_reverted FROM landing.editor_state WHERE user_key = 'Alice'"
        ).fetchone()
    assert replayed["editors"]["Alice"]["edits_reverted"] == 1
    assert stored is not None and stored["edits_reverted"] == 0

    # Applying the discovered reverts is what closes it, without a replay and
    # without overwriting anything.
    with connect() as conn:
        result = state.apply_reverts(conn, days=30)
    assert result == {
        "applied": 1,
        "counters_moved": 1,
        "no_row_to_move": 0,
        "partly_applied": 0,
        "retried": 0,
    }

    assert reconcile.run(days=7)["divergences"] == 0


@pytest.mark.db
def test_a_revert_is_folded_in_exactly_once(fresh_db: None) -> None:
    """Not zero times, which was the bug. Not twice, which would be worse,
    because nothing would ever say so.

    An edit can be named by several revert_events and by a label besides. They
    all describe one fact.
    """
    with connect() as conn:
        _event(conn, 1, minutes_ago=200)
        conn.execute(
            """
            INSERT INTO outcome.revert_events (revert_revid, reverted_revid, revert_ts, method)
            VALUES (2, 1, now() - make_interval(mins => 150), 'mw-undo'),
                   (3, 1, now() - make_interval(mins => 140), 'mw-rollback')
            """
        )
        conn.execute(
            """
            INSERT INTO outcome.labels
                (revid, label, label_source, first_observed_at_utc, detection_latency_seconds)
            VALUES (1, true, 'mw_reverted', now() - make_interval(mins => 130), 900)
            """
        )
        _score_the_batch(conn)

        assert state.apply_reverts(conn)["applied"] == 1
        # Every subsequent run must be a no-op, however often it runs.
        assert state.apply_reverts(conn)["applied"] == 0
        assert state.apply_reverts(conn)["applied"] == 0

        row = conn.execute(
            "SELECT edits_reverted FROM landing.editor_state WHERE user_key = 'Alice'"
        ).fetchone()
    assert row is not None and row["edits_reverted"] == 1


@pytest.mark.db
def test_a_revert_nobody_has_discovered_yet_is_not_folded_in(fresh_db: None) -> None:
    """The leak this whole change exists to close.

    The revert HAPPENED two hours ago. This system does not find out for another
    hour. Folding it in now would build a history out of knowledge production
    could not have had — quieter than a leaked column, and it survives the
    knowability guard untouched, because no feature depends on the future of the
    event it describes. The state does.
    """
    with connect() as conn:
        _event(conn, 1, minutes_ago=200)
        conn.execute(
            """
            INSERT INTO outcome.revert_events
                (revert_revid, reverted_revid, revert_ts, method, observed_at_utc)
            VALUES (2, 1, now() - make_interval(mins => 120), 'mw-undo',
                    now() + make_interval(mins => 60))
            """
        )
        assert state.apply_reverts(conn)["applied"] == 0

        replayed = state.replay(conn, days=7)["state"]
    assert replayed["editors"]["Alice"]["edits_reverted"] == 0


# ---------------------------------------------------------------------------
# A revert whose row was not there yet
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_a_revert_with_no_row_yet_is_applied_once_the_row_appears(fresh_db: None) -> None:
    """The seventeen-of-forty that were dropped every run.

    A revert can be discovered before its editor has ever been folded — the
    edit sits outside the scorer's lookback, or inside the champion's training
    window, which the scorer refuses to score and therefore never folds. The
    increment had nowhere to land, and the revert was recorded as handled, so
    it was never applied even after the editor edited again and got a row.

    The replay folds it regardless, because the edit is in rc_events. That gap
    is the editor_edits_reverted and page.reverted divergence reconcile
    measures.
    """
    with connect() as conn:
        _event(conn, 1, minutes_ago=200)
        conn.execute(
            """
            INSERT INTO outcome.revert_events (revert_revid, reverted_revid, revert_ts, method)
            VALUES (2, 1, now() - make_interval(mins => 150), 'mw-undo')
            """
        )

        # Discovered before anything was folded: no editor_state row exists.
        first = state.apply_reverts(conn)
        assert first["applied"] == 1
        assert first["no_row_to_move"] == 1
        assert first["counters_moved"] == 0

        # The editor edits again and is scored, so the row now exists — at zero,
        # because scoring never knows an outcome.
        _score_the_batch(conn)
        row = conn.execute(
            "SELECT edits_reverted FROM landing.editor_state WHERE user_key = 'Alice'"
        ).fetchone()
        assert row["edits_reverted"] == 0

        # The retry is the fix: previously this revert was marked handled and
        # would never be looked at again.
        second = state.apply_reverts(conn)
        assert second["retried"] == 1
        assert second["counters_moved"] == 1

        row = conn.execute(
            "SELECT edits_reverted FROM landing.editor_state WHERE user_key = 'Alice'"
        ).fetchone()
        assert row["edits_reverted"] == 1

        # And it must not keep applying. Retrying forever would be the other bug.
        assert state.apply_reverts(conn)["applied"] == 0
        row = conn.execute(
            "SELECT edits_reverted FROM landing.editor_state WHERE user_key = 'Alice'"
        ).fetchone()
        assert row["edits_reverted"] == 1


@pytest.mark.db
def test_a_revert_that_moved_only_one_side_is_not_recorded_as_done(fresh_db: None) -> None:
    """The defect the old counter could not express.

    apply_reverts summed the editor and page rowcounts into one `touched`, so a
    revert that incremented the editor but found no page row counted as moved
    and was marked fully handled. The page counter stayed short forever while
    the record claimed success.
    """
    with connect() as conn:
        _event(conn, 1, minutes_ago=200)
        _score_the_batch(conn)

        # Remove the page row, leaving the editor's in place: one side present,
        # one absent, which is what a partly-folded pair looks like.
        conn.execute("DELETE FROM landing.page_state")
        conn.execute(
            """
            INSERT INTO outcome.revert_events (revert_revid, reverted_revid, revert_ts, method)
            VALUES (2, 1, now() - make_interval(mins => 150), 'mw-undo')
            """
        )

        first = state.apply_reverts(conn)
        assert first["partly_applied"] == 1, "one side landed, one did not"
        assert first["counters_moved"] == 0, "a half-applied revert is not applied"

        marked = conn.execute(
            "SELECT editor_moved, page_moved FROM landing.state_applied_reverts WHERE revid = 1"
        ).fetchone()
        assert marked["editor_moved"] is True
        assert marked["page_moved"] is False

        # The editor side must not be applied a second time by the retry.
        _score_the_batch(conn)
        second = state.apply_reverts(conn)
        assert second["counters_moved"] == 1

        editor = conn.execute(
            "SELECT edits_reverted FROM landing.editor_state WHERE user_key = 'Alice'"
        ).fetchone()
        page = conn.execute(
            "SELECT edits_reverted FROM landing.page_state WHERE page_key = 'Page'"
        ).fetchone()
    assert editor["edits_reverted"] == 1, "counted once, not twice"
    assert page["edits_reverted"] == 1


@pytest.mark.db
def test_a_pre_migration_row_is_never_retried(fresh_db: None) -> None:
    """NULL means unknown, and unknown must not be re-applied.

    Rows written before sql/033 record only whether SOMETHING moved, so a retry
    could re-apply a revert already counted. Under-counting a known amount is
    recoverable; double-counting silently is not.
    """
    with connect() as conn:
        _event(conn, 1, minutes_ago=200)
        _score_the_batch(conn)
        conn.execute(
            """
            INSERT INTO outcome.revert_events (revert_revid, reverted_revid, revert_ts, method)
            VALUES (2, 1, now() - make_interval(mins => 150), 'mw-undo')
            """
        )
        # Exactly what the old code left behind: handled, one flag, both new
        # columns NULL.
        conn.execute(
            "INSERT INTO landing.state_applied_reverts (revid, counters_moved) VALUES (1, true)"
        )

        assert state.apply_reverts(conn)["applied"] == 0, "NULL rows are left alone"

        row = conn.execute(
            "SELECT edits_reverted FROM landing.editor_state WHERE user_key = 'Alice'"
        ).fetchone()
    assert row["edits_reverted"] == 0, "not re-applied on top of whatever it already did"
