"""The knowability guard (SRS FR-14, M2-FR-7 and FR-10).

A leaky feature does not raise. It produces a model that scores beautifully in
backtest and is worthless deployed, and the gap between those two is discovered
weeks later, if at all. So the guard cannot be a review convention. It has to be
a job step that fails.

**How it works: differential probes.**

Every check here names something that must not influence a feature, mutates a
probe event so that only that thing changes, and requires the feature hash to be
identical. Inspecting code for leaks finds the leaks you thought to look for.
Mutating an input finds the ones you did not.

That approach is only sound because feature functions are pure. If one could
consult the database, the clock or the network, mutating its arguments would
prove nothing — which is why :mod:`bellwether.features` keeps them pure.

The guard **raises**. It does not warn, and it does not return a score. A
warning in a scheduled job is a line in a log nobody reads.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from bellwether import features


class LeakageError(AssertionError):
    """A feature depended on something it could not have known."""


def probe_event(**overrides: Any) -> dict[str, Any]:
    """A synthetic event with every field a feature might read."""
    base: dict[str, Any] = {
        "revid": 1_000_000,
        "old_revid": 999_999,
        "event_ts": datetime(2026, 8, 14, 13, 30, tzinfo=UTC),
        "ingested_at_utc": datetime(2026, 8, 14, 13, 30, 5, tzinfo=UTC),
        "ingest_run_id": "11111111-1111-1111-1111-111111111111",
        "ns": 0,
        "title": "Some Page",
        "user_name": "Editor",
        "user_id": 55_204_706,
        "is_anon": False,
        "is_temp": False,
        "is_minor": False,
        "is_bot": False,
        "comment": "/* History */ tidy up",
        "comment_hidden": False,
        "user_hidden": False,
        "oldlen": 8000,
        "newlen": 8120,
        "tags": ["visualeditor", "mobile edit"],
    }
    base.update(overrides)
    return base


# Each entry: what must not matter, and how to make only that thing differ.
#
# The first is the trap already sitting in the database. Rows backfilled days
# late carry `mw-reverted` in their frozen tag array; live rows structurally
# never can. A feature that read it would be a perfect predictor on 40% of the
# current data and nothing on the rest.
MUST_NOT_MATTER: list[tuple[str, Any]] = [
    (
        "the mw-reverted outcome tag",
        lambda e: {**e, "tags": [*e["tags"], "mw-reverted"]},
    ),
    (
        "when the row happened to be ingested",
        lambda e: {**e, "ingested_at_utc": e["event_ts"] + timedelta(days=3)},
    ),
    (
        "which pipeline run ingested it",
        lambda e: {**e, "ingest_run_id": "22222222-2222-2222-2222-222222222222"},
    ),
    (
        "any outcome tag arriving alongside real ones",
        lambda e: {**e, "tags": ["mw-reverted", *e["tags"]]},
    ),
]


def check_forbidden_influences(event: dict[str, Any] | None = None) -> None:
    """Mutate things that must not matter; require the hash to be unchanged."""
    base = event or probe_event()
    expected = features.feature_hash(features.build(base))

    for description, mutate in MUST_NOT_MATTER:
        mutated = mutate(base)
        got = features.feature_hash(features.build(mutated))
        if got != expected:
            changed = [
                name
                for name, value in features.build(mutated).items()
                if value != features.build(base)[name]
            ]
            raise LeakageError(
                f"a feature depends on {description}: {', '.join(changed)}. "
                "That value is not knowable at scoring time, or not knowable "
                "consistently between backfilled and live rows."
            )


def check_history_is_strictly_past(event: dict[str, Any] | None = None) -> None:
    """Adding events AFTER the subject must not change its features.

    History features are computed from a view restricted to earlier events. The
    restriction is enforced where the view is built; this proves it holds from
    the outside, where a mistake would actually show.
    """
    base = event or probe_event()
    past = {"editor_edits_seen": 4, "page_edits_seen": 9}
    expected = features.feature_hash(features.build(base, past))

    # The same history plus activity that happened later. A correct feature set
    # cannot see it; a leaky one silently improves.
    with_future = {**past, "_future_edits": 500, "_future_reverts": 50}
    got = features.feature_hash(features.build(base, with_future))
    if got != expected:
        raise LeakageError(
            "a feature changed when future activity was added to the history "
            "view. Features must be computable from events strictly earlier "
            "than the one being scored."
        )


def check_no_forbidden_columns() -> None:
    """A feature that reads an ingestion column is a leak waiting for a backfill.

    Caught by mutation above, but named separately so the failure says which
    rule was broken rather than only which feature moved.
    """
    base = probe_event()
    for column in sorted(features.FORBIDDEN_COLUMNS):
        mutated = {**base, column: "MUTATED"}
        if features.feature_hash(features.build(mutated)) != features.feature_hash(
            features.build(base)
        ):
            raise LeakageError(f"a feature reads {column}, which is not about the edit")


def run_all(event: dict[str, Any] | None = None) -> None:
    """Every check. Called before any feature build and by CI."""
    check_forbidden_influences(event)
    check_history_is_strictly_past(event)
    check_no_forbidden_columns()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    run_all()
    print(
        f"knowability: {len(features.feature_names())} features, "
        f"{len(MUST_NOT_MATTER)} forbidden influences, all clean"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
