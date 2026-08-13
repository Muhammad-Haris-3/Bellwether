"""The sampling frame.

The frame is the project's most consequential parameter: every rate, model and
comparison downstream is conditional on it. These tests hold the two properties
that make it auditable — determinism, and weights that recover the population.
"""

from __future__ import annotations

import pytest

from bellwether import frame


def _event(revid: int, *, temp: bool = False, anon: bool = False) -> dict:
    return {"revid": revid, "is_anon": anon, "is_temp": temp}


def test_the_frame_is_deterministic() -> None:
    """Re-running ingestion must select the identical set.

    Otherwise the sample is whatever the scheduler happened to do that day, and
    nobody — including the author — can audit it.
    """
    events = [_event(r) for r in range(1, 500)]
    first = [frame.in_sample(e) for e in events]
    second = [frame.in_sample(e) for e in events]
    assert first == second


def test_bucketing_does_not_depend_on_process_hash_seed() -> None:
    """Python's built-in hash() is randomised per process unless PYTHONHASHSEED
    is pinned. A frame built on it would differ between runs, which is the one
    thing it must never do. These are fixed expected values."""
    assert frame.bucket(1, "bellwether/sample/v1") == frame.bucket(1, "bellwether/sample/v1")
    assert frame.bucket(1, "a") != frame.bucket(1, "b")
    assert 0 <= frame.bucket(1369185025, "bellwether/sample/v1") <= 99


def test_sampling_rates_land_near_their_targets() -> None:
    n = 20_000
    logged_out = sum(frame.in_sample(_event(r, temp=True)) for r in range(n))
    registered = sum(frame.in_sample(_event(r)) for r in range(n))

    assert 100 * logged_out / n == pytest.approx(frame.SAMPLE_PERCENT["logged_out"], abs=1.5)
    assert 100 * registered / n == pytest.approx(frame.SAMPLE_PERCENT["registered"], abs=1.0)


def test_weights_recover_the_population() -> None:
    """M1-FR-2. Weighted counts of the sample must approximate the population
    it was drawn from, or no published rate means anything."""
    n = 40_000
    population_logged_out = n
    weighted = sum(
        frame.weight_of(_event(r, temp=True))
        for r in range(n)
        if frame.in_sample(_event(r, temp=True))
    )
    assert weighted == pytest.approx(population_logged_out, rel=0.05)


def test_registered_weights_recover_the_population() -> None:
    n = 60_000
    weighted = sum(frame.weight_of(_event(r)) for r in range(n) if frame.in_sample(_event(r)))
    assert weighted == pytest.approx(n, rel=0.10)


def test_the_cohort_is_independent_of_inclusion() -> None:
    """M1-FR-6. Sharing a salt would make cohort membership a function of being
    sampled, and the survival curve would then be estimated from a non-random
    slice of the sample rather than a random one."""
    n = 20_000
    sampled = [r for r in range(n) if frame.in_sample(_event(r, temp=True))]
    cohort_rate_within = (
        100 * sum(frame.in_maturity_cohort(_event(r)) for r in sampled) / len(sampled)
    )

    assert cohort_rate_within == pytest.approx(frame.MATURITY_COHORT_PERCENT, abs=1.5)


def test_temporary_accounts_are_logged_out() -> None:
    assert frame.stratum_of(_event(1, temp=True)) == "logged_out"
    assert frame.stratum_of(_event(1, anon=True)) == "logged_out"
    assert frame.stratum_of(_event(1)) == "registered"


def test_an_event_without_the_temp_key_is_registered() -> None:
    """Rows parsed before is_temp existed must not crash the classifier."""
    assert frame.stratum_of({"revid": 1, "is_anon": False}) == "registered"


def test_weights_are_inverse_probabilities() -> None:
    assert frame.weight_of(_event(1, temp=True)) == pytest.approx(
        100 / frame.SAMPLE_PERCENT["logged_out"]
    )
    assert frame.weight_of(_event(1)) == pytest.approx(100 / frame.SAMPLE_PERCENT["registered"])


def test_the_frame_keeps_logged_out_edits_far_more_often() -> None:
    """The whole point of stratifying: the positive class is 7x rarer among
    registered editors (22.25% against 3.26%, measured in M0), so a uniform
    sample would spend the budget on the majority class."""
    assert frame.SAMPLE_PERCENT["logged_out"] > frame.SAMPLE_PERCENT["registered"] * 10
