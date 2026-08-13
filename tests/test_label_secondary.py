"""Secondary label path.

Conservative by design: it must never claim a revert that did not happen, and
it is allowed to miss ones that did. These tests hold that asymmetry in place.
"""

from __future__ import annotations

from typing import Any

import pytest

from bellwether.db import connect
from bellwether.label_secondary import reverted_revid_for, run

pytestmark = pytest.mark.db


def test_undo_target_comes_from_the_summary_not_old_revid() -> None:
    """Undoing an older revision while keeping later ones leaves old_revid
    pointing at an edit that was not reverted at all."""
    edit = {
        "tags": ["mw-undo"],
        "old_revid": 999,
        "comment": "Undo revision 12345 by [[Special:Contributions/1.2.3.4|1.2.3.4]]",
    }
    assert reverted_revid_for(edit) == 12345


def test_undid_spelling_is_also_matched() -> None:
    edit = {"tags": ["mw-undo"], "old_revid": 999, "comment": "Undid revision 777 by Someone"}
    assert reverted_revid_for(edit) == 777


def test_the_linked_diff_summary_format_is_matched() -> None:
    """The shape English Wikipedia actually emits, copied from live data.

    Measured 2026-08-13: 97 of 97 undo summaries used this form and none used
    the bare number. A pattern that handles only the bare form derives nothing
    from any undo while reporting a clean run.
    """
    edit = {
        "tags": ["mw-undo"],
        "old_revid": 999,
        "comment": (
            "Undid revision [[Special:Diff/1367883184|1367883184]] by "
            "[[Special:Contributions/1.2.3.4|1.2.3.4]]"
        ),
    }
    assert reverted_revid_for(edit) == 1367883184


def test_an_undo_with_no_parsable_summary_is_skipped_not_guessed() -> None:
    """Counted and reported rather than filled in from old_revid, which would
    be wrong precisely in the cases the summary was rewritten."""
    edit = {"tags": ["mw-undo"], "old_revid": 999, "comment": "rv"}
    assert reverted_revid_for(edit) is None


def test_rollback_uses_old_revid() -> None:
    edit = {"tags": ["mw-rollback"], "old_revid": 555, "comment": "Reverted edits by X"}
    assert reverted_revid_for(edit) == 555


def test_manual_revert_uses_old_revid() -> None:
    edit = {"tags": ["mw-manual-revert"], "old_revid": 556, "comment": ""}
    assert reverted_revid_for(edit) == 556


def test_an_ordinary_edit_yields_nothing() -> None:
    assert reverted_revid_for({"tags": ["visualeditor"], "old_revid": 1, "comment": "x"}) is None


def _seed(conn: Any, revid: int, age_seconds: int, **kw: Any) -> None:
    conn.execute(
        """
        INSERT INTO landing.rc_events
            (revid, old_revid, event_ts, ns, title, is_anon, is_temp, is_minor, is_bot,
             comment, tags, sampling_stratum, ingested_at_utc)
        VALUES (%(revid)s, %(old_revid)s, now() - make_interval(secs => %(age)s), 0, 'Page',
                false, false, false, false, %(comment)s, %(tags)s, 'registered', now())
        """,
        {
            "revid": revid,
            "old_revid": kw.get("old_revid"),
            "age": age_seconds,
            "comment": kw.get("comment", ""),
            "tags": kw.get("tags", []),
        },
    )


def _observe_revert(conn: Any, revert_revid: int, reverted_revid: int, age_seconds: int) -> None:
    """Record a reverting edit the way ingestion now does.

    Reverting edits are captured for the whole feed, outside the sampling
    frame — 93.8% of them are made by registered editors, whom the frame
    samples at 3%, so deriving them from rc_events would have made this path
    blind to almost everything it exists to see.
    """
    conn.execute(
        """
        INSERT INTO outcome.revert_events
            (revert_revid, reverted_revid, revert_ts, method)
        VALUES (%s, %s, now() - make_interval(secs => %s), 'mw-rollback')
        ON CONFLICT (revert_revid) DO NOTHING
        """,
        (revert_revid, reverted_revid, age_seconds),
    )


def test_a_rollback_labels_the_edit_it_reverted(fresh_db: None) -> None:
    with connect() as conn:
        _seed(conn, 100, age_seconds=7_200)  # the bad edit
        _observe_revert(conn, revert_revid=101, reverted_revid=100, age_seconds=3_600)

    result = run(lookback_hours=48)
    assert result["labels_written"] == 1

    with connect() as conn:
        row = conn.execute(
            "SELECT label, revert_revid, revert_latency_seconds "
            "FROM outcome.labels WHERE revid = 100 AND label_source = 'revert_tag'"
        ).fetchone()

    assert row is not None
    assert row["label"] is True
    assert row["revert_revid"] == 101
    assert row["revert_latency_seconds"] == pytest.approx(3600, abs=5)


def test_a_revert_cannot_precede_what_it_reverts(fresh_db: None) -> None:
    """A mis-parsed summary naming an unrelated later revision would otherwise
    produce a negative latency and a silently wrong label."""
    with connect() as conn:
        _seed(conn, 200, age_seconds=60)  # "reverted" edit is NEWER than the revert
        _observe_revert(conn, revert_revid=201, reverted_revid=200, age_seconds=3_600)

    assert run(lookback_hours=48)["labels_written"] == 0


def test_an_unseen_target_is_not_labelled(fresh_db: None) -> None:
    """Reverting edits are observed for the whole feed, so most of them point
    at edits the frame did not sample. Those are recorded as revert events and
    produce no label — there is nothing here to attach one to."""
    with connect() as conn:
        _observe_revert(conn, revert_revid=301, reverted_revid=300, age_seconds=3_600)

    assert run(lookback_hours=48)["labels_written"] == 0


def test_the_run_is_idempotent(fresh_db: None) -> None:
    with connect() as conn:
        _seed(conn, 400, age_seconds=7_200)
        _observe_revert(conn, revert_revid=401, reverted_revid=400, age_seconds=3_600)

    assert run(lookback_hours=48)["labels_written"] == 1
    assert run(lookback_hours=48)["labels_written"] == 0


def test_the_secondary_path_makes_no_api_calls(fresh_db: None) -> None:
    """Its cheapness is a design property, not an accident — it is what lets it
    label the recent tail long before the primary path's grid reaches it."""
    with connect() as conn:
        _seed(conn, 500, age_seconds=7_200)
        _observe_revert(conn, revert_revid=501, reverted_revid=500, age_seconds=3_600)

    assert run(lookback_hours=48)["api_calls"] == 0
