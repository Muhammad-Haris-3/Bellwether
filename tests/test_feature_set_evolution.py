"""Changing the feature set without falsifying the past.

PREREGISTRATION §11 leaves the feature set free to change while the rule that
judges it stays fixed. In practice it was not free, and these tests pin the
three reasons it was not:

  * every model was scored against `features.feature_names()` rather than the
    list it was registered with, so one added feature changed the WIDTH and —
    because that list is sorted — the ORDER of the champion's input columns;
  * the challenger was handed the champion's row, so a challenger with a
    different feature set would raise on every event, be swallowed as "no
    opinion", and never accumulate a paired observation the promotion rule
    could use;
  * `feature_hash` digested the whole vector, so adding a feature made every
    historical prediction fail to re-derive and fired the watchdog's
    reproducibility fault over a change that broke nothing.

The property that matters most is the last one, and it is the one worth stating
plainly: a prediction's hash must depend on that prediction, and on nothing
that happened afterwards.
"""

from __future__ import annotations

import pytest

from bellwether import features

BASE_VECTOR = {
    "is_logged_out": 1.0,
    "byte_delta": -420.0,
    "abs_byte_delta": 420.0,
    "account_newness": 0.98,
    "editor_edits_seen": 0.0,
}


# ---------------------------------------------------------------------------
# The hash
# ---------------------------------------------------------------------------


def test_hashing_a_subset_ignores_features_added_later():
    """The property the whole change exists for.

    A model registered on today's features must keep re-deriving the same hash
    after the feature set grows. Without this, the reproduction job reports the
    entire register as unreproducible the day a feature lands.
    """
    registered = sorted(BASE_VECTOR)
    before = features.feature_hash(BASE_VECTOR, registered)

    grown = {**BASE_VECTOR, "byte_delta_ratio": -0.83, "page_revert_rate": 0.2}
    after = features.feature_hash(grown, registered)

    assert before == after


def test_hashing_the_whole_vector_is_what_used_to_break():
    """The negative control, so the test above cannot pass vacuously."""
    grown = {**BASE_VECTOR, "byte_delta_ratio": -0.83}
    assert features.feature_hash(BASE_VECTOR) != features.feature_hash(grown)


def test_the_default_is_unchanged_so_stored_hashes_still_verify():
    """Backward compatibility, stated as an assertion rather than a hope.

    Every hash already in the register was computed over the full vector, by a
    model whose registered list was the full set then in force. Passing that
    list must reproduce the old digest exactly, or this change silently
    invalidates the existing evidence.
    """
    assert features.feature_hash(BASE_VECTOR) == features.feature_hash(
        BASE_VECTOR, sorted(BASE_VECTOR)
    )


def test_a_missing_feature_raises_rather_than_hashing_a_short_vector():
    """A model asking for a feature this build cannot produce is incompatible.

    Skipping it would produce a plausible digest for the wrong inputs, and the
    mismatch would surface days later as an unexplained reproducibility
    failure.
    """
    with pytest.raises(KeyError):
        features.feature_hash(BASE_VECTOR, [*BASE_VECTOR, "a_feature_that_was_removed"])


def test_subset_order_does_not_change_the_digest():
    """The hash sorts internally, so it digests inputs rather than column order.

    Column ORDER still matters to the model and is taken from the registered
    list; it must not also perturb the hash, or a reordering would read as a
    reproducibility failure.
    """
    names = sorted(BASE_VECTOR)
    assert features.feature_hash(BASE_VECTOR, names) == features.feature_hash(
        BASE_VECTOR, list(reversed(names))
    )


# ---------------------------------------------------------------------------
# The ordering hazard the sorted global list created
# ---------------------------------------------------------------------------


def test_feature_names_is_sorted_so_an_insertion_would_permute_columns():
    """Why the registered list is used for column order, not the live one.

    `feature_names()` is sorted, so a new feature does not append — it inserts.
    Adding `byte_delta_ratio` lands directly after `byte_delta` and shifts every
    column after it. A model fed that row is not merely getting one extra input;
    it is getting most of its inputs in the wrong places, and it would carry on
    producing confident, meaningless scores.
    """
    names = features.feature_names()
    assert names == sorted(names)

    grown = sorted([*names, "byte_delta_ratio"])
    insertion = grown.index("byte_delta_ratio")
    assert insertion < len(names) - 1, "expected an insertion, not an append"
    assert grown[insertion + 1 :] != names[insertion + 1 :]


def test_every_registered_name_is_still_produced():
    """The live feature set must cover what feature_names promises.

    Cheap, and it is the check the scorer performs against a real registry
    before it will score anything.
    """
    vector = features.build({"event_ts": None}, {})
    assert set(vector) == set(features.feature_names())
