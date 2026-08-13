"""The knowability guard, including deliberately planted leaks (C-3, C-4).

A guard that has never caught anything is indistinguishable from one that
cannot. Each planted leak below is a feature a reasonable person might write,
and each must make the guard raise.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from bellwether import features, knowability
from bellwether.knowability import LeakageError, probe_event


@pytest.fixture
def restore_features() -> Iterator[None]:
    """Planted leaks mutate the module-level feature set. Put it back."""
    event_before = dict(features.EVENT_FEATURES)
    history_before = dict(features.HISTORY_FEATURES)
    yield
    features.EVENT_FEATURES.clear()
    features.EVENT_FEATURES.update(event_before)
    features.HISTORY_FEATURES.clear()
    features.HISTORY_FEATURES.update(history_before)


def test_the_real_feature_set_is_clean() -> None:
    knowability.run_all()


def test_a_planted_outcome_tag_leak_is_caught(restore_features: None) -> None:
    """C-3, and the trap that is already in the database.

    Backfilled rows carry mw-reverted in their frozen tag array; live rows
    structurally never can. This feature would be a perfect predictor on 40% of
    the current data and worthless on the rest — the most dangerous thing that
    could be written against this schema, and it looks entirely reasonable.
    """
    features.EVENT_FEATURES["was_reverted"] = lambda e, h: float(
        "mw-reverted" in (e.get("tags") or [])
    )

    with pytest.raises(LeakageError, match="mw-reverted"):
        knowability.check_forbidden_influences()


def test_a_leak_through_an_unfiltered_tag_count_is_caught(restore_features: None) -> None:
    """The subtler version: no mention of mw-reverted at all.

    Counting raw tags instead of `_safe_tags` leaks the outcome through the
    length of the array. Nothing in the code names the label.
    """
    features.EVENT_FEATURES["raw_tag_count"] = lambda e, h: float(len(e.get("tags") or []))

    with pytest.raises(LeakageError):
        knowability.check_forbidden_influences()


def test_a_leak_through_ingestion_lag_is_caught(restore_features: None) -> None:
    """ "How long after the edit did we fetch it" is a perfect proxy for
    backfilled-ness, and backfilled rows are the ones whose tags show reverts."""
    features.EVENT_FEATURES["ingest_lag"] = lambda e, h: float(
        (e["ingested_at_utc"] - e["event_ts"]).total_seconds()
    )

    with pytest.raises(LeakageError, match="ingested"):
        knowability.check_forbidden_influences()


def test_a_feature_reading_the_run_id_is_caught(restore_features: None) -> None:
    features.EVENT_FEATURES["run_marker"] = lambda e, h: float(
        len(str(e.get("ingest_run_id") or ""))
    )
    with pytest.raises(LeakageError):
        knowability.check_no_forbidden_columns()


def test_a_history_feature_reading_the_future_is_caught(restore_features: None) -> None:
    """SRS Threat 1, in the shape it actually appears.

    A history view accidentally built without the `event_ts <` restriction
    returns the editor's whole record, including edits made after the one being
    scored. The feature looks unremarkable.
    """
    features.HISTORY_FEATURES["editor_activity"] = lambda e, h: float(
        h.get("editor_edits_seen", 0) + h.get("_future_edits", 0)
    )

    with pytest.raises(LeakageError, match="future"):
        knowability.check_history_is_strictly_past()


def test_run_all_catches_a_leak_in_any_check(restore_features: None) -> None:
    features.EVENT_FEATURES["was_reverted"] = lambda e, h: float(
        "mw-reverted" in (e.get("tags") or [])
    )
    with pytest.raises(LeakageError):
        knowability.run_all()


def test_reverting_tags_are_not_forbidden() -> None:
    """mw-undo describes what THIS edit did and is applied when it is saved.

    Forbidding it would throw away real signal in the name of safety. The line
    is drawn at tags applied afterwards, not at anything revert-shaped.
    """
    reverting = probe_event(tags=["mw-undo"])
    ordinary = probe_event(tags=["visualeditor"])

    assert features.build(reverting)["is_reverting"] == 1.0
    assert features.build(ordinary)["is_reverting"] == 0.0
    knowability.run_all()


def test_the_feature_hash_is_stable_across_runs() -> None:
    """SRS FR-15. A prediction that cannot be reproduced cannot be audited."""
    event = probe_event()
    assert features.feature_hash(features.build(event)) == features.feature_hash(
        features.build(event)
    )


def test_the_feature_hash_changes_when_the_edit_changes() -> None:
    small = probe_event(newlen=8010)
    large = probe_event(newlen=20000)
    assert features.feature_hash(features.build(small)) != features.feature_hash(
        features.build(large)
    )


def test_every_feature_is_finite_on_awkward_input() -> None:
    """Missing lengths, no comment, no parent revision, anonymous editor.

    A NaN reaches the model as a silent row drop or a crash at training time,
    and either way it is discovered somewhere far from here.
    """
    awkward = probe_event(
        old_revid=None,
        oldlen=None,
        newlen=None,
        comment=None,
        user_id=None,
        is_anon=True,
        tags=[],
        event_ts=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=1),
    )
    for name, value in features.build(awkward).items():
        assert value == value, f"{name} is NaN"
        assert abs(value) != float("inf"), f"{name} is infinite"


def test_logged_out_covers_temporary_accounts() -> None:
    """The M0 finding, as a feature. English Wikipedia masks IPs behind
    temporary accounts, so `anon` is never set there."""
    assert features.build(probe_event(is_temp=True))["is_logged_out"] == 1.0
    assert features.build(probe_event(is_anon=True))["is_logged_out"] == 1.0
    assert features.build(probe_event())["is_logged_out"] == 0.0
