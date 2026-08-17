"""Transfer accounting.

NFR-4 caps storage and bellwether.retention reports against it. Nothing capped
transfer, which is a second Neon Free allowance with a worse failure mode:
storage exhaustion refuses writes, transfer exhaustion refuses connections, so
every job and the API stop at once.

GridCast — same plan, same architecture, no counter — lost roughly two weeks of
register growth to it. This is the counter that project did not have, so what
is tested is the two things it has to get right:

  * it counts rows that CROSS THE WIRE, not rows scanned — otherwise a
    server-side aggregate looks as expensive as the table it aggregates, and
    the cheapest fix available looks like the most expensive query here;
  * it never takes down the job it is measuring.

The byte figure is an estimate and its accuracy is not tested, because it has
none. What is tested is that it moves with the size of what came back, which is
the only property the trend depends on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from bellwether.usage import (
    DECLINE_FRACTION,
    FIELD_OVERHEAD_BYTES,
    ROW_OVERHEAD_BYTES,
    WARN_FRACTION,
    Meter,
    human_bytes,
    period_start,
    record_run,
    value_width,
)

# ---------------------------------------------------------------------------
# What the meter counts
# ---------------------------------------------------------------------------


def test_a_wide_read_costs_more_than_a_narrow_one():
    """The estimate must move with what came back, or the trend is noise."""
    narrow, wide = Meter(), Meter()
    narrow.record([{"revid": i} for i in range(100)])
    wide.record([{"revid": i, "title": "A" * 200} for i in range(100)])

    assert wide.bytes_estimated > narrow.bytes_estimated * 5


def test_an_aggregate_costs_one_row_however_much_it_scanned():
    """The argument for pushing work into SQL, asserted.

    Several queries here reduce a period of rc_events to a handful of counts. If
    the meter charged for rows scanned, those would look like the most expensive
    reads in the project and the pressure would run the wrong way — towards
    pulling rows to a runner, which is what actually costs the allowance.
    """
    aggregate, materialised = Meter(), Meter()
    aggregate.record([{"count": 250_000}])
    materialised.record([{"revid": i} for i in range(250_000)])

    assert aggregate.bytes_estimated < materialised.bytes_estimated / 1000


def test_an_empty_result_still_counts_as_a_query():
    """A query that returned nothing still made a round trip."""
    meter = Meter()
    meter.record([])
    assert (meter.queries, meter.rows) == (1, 0)


def test_totals_accumulate_across_queries():
    meter = Meter()
    meter.record([{"a": 1}, {"a": 2}])
    meter.record([{"a": 3}])
    assert (meter.queries, meter.rows) == (2, 3)


def test_row_overhead_is_charged_once_per_row():
    """Two one-field rows cost one row's overhead more than one two-field row."""
    split, together = Meter(), Meter()
    split.record([{"a": 1}, {"b": 2}])
    together.record([{"a": 1, "b": 2}])
    assert split.bytes_estimated - together.bytes_estimated == ROW_OVERHEAD_BYTES


# ---------------------------------------------------------------------------
# Width estimation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [None, True, 42, 3.5, "text", b"bytes", Decimal("1.25"), uuid.uuid4(), datetime.now(UTC)],
)
def test_every_supported_type_has_a_positive_width(value):
    assert value_width(value) >= FIELD_OVERHEAD_BYTES


def test_a_long_string_costs_more_than_a_short_one():
    """Variable-width columns are measured, not given a flat cost.

    landing.rc_events holds tag arrays and page titles alongside integer ids,
    and a flat per-column cost would make reading one indistinguishable from
    reading the other.
    """
    assert value_width("x" * 1000) > value_width("x") + 900


def test_an_unknown_type_is_still_counted():
    """An unmapped type must not silently cost nothing."""

    class Odd:
        def __str__(self) -> str:
            return "a moderately long representation"

    assert value_width(Odd()) > FIELD_OVERHEAD_BYTES


# ---------------------------------------------------------------------------
# The billing period
# ---------------------------------------------------------------------------


def test_the_period_follows_the_configured_reset_day(monkeypatch):
    """Measured over the window that actually resets, not the calendar month.

    Where the reset falls mid-month a calendar total is wrong in both
    directions at once: it counts bytes already forgiven and misses bytes that
    are not.
    """
    from bellwether import config

    config.get_settings.cache_clear()
    monkeypatch.setenv("BELLWETHER_BILLING_PERIOD_DAY", "12")
    try:
        assert period_start(datetime(2026, 8, 20, tzinfo=UTC)) == datetime(2026, 8, 12, tzinfo=UTC)
        assert period_start(datetime(2026, 8, 3, tzinfo=UTC)) == datetime(2026, 7, 12, tzinfo=UTC)
        assert period_start(datetime(2026, 1, 5, tzinfo=UTC)) == datetime(2025, 12, 12, tzinfo=UTC)
    finally:
        config.get_settings.cache_clear()


def test_thresholds_are_ordered():
    """Warning before standing down, or the warning never arrives."""
    assert 0 < WARN_FRACTION < DECLINE_FRACTION < 1


# ---------------------------------------------------------------------------
# Not breaking the job it measures
# ---------------------------------------------------------------------------


def test_recording_swallows_a_database_failure(monkeypatch, capsys):
    """Accounting that can fail the job it accounts for is worse than none.

    The specific case is unmissable: this table lives in the database whose
    exhaustion the counter exists to predict, so it is unreachable at exactly
    the moment it is most interesting.
    """
    from bellwether import usage

    monkeypatch.setattr(usage.METER, "queries", 1, raising=False)

    def explode(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("bellwether.db.connect", explode)

    record_run("probe")  # must not raise
    assert "could not record transfer" in capsys.readouterr().out


def test_recording_is_skipped_when_nothing_was_read(monkeypatch):
    """A job that read nothing writes no row, rather than a row of zeroes."""
    from bellwether import usage

    monkeypatch.setattr(usage.METER, "queries", 0, raising=False)

    def explode(*args, **kwargs):
        raise AssertionError("connect must not be called")

    monkeypatch.setattr("bellwether.db.connect", explode)
    record_run("probe")


# ---------------------------------------------------------------------------
# The state the watchdog alerts on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fraction", "expected"),
    [
        (0.0, "ok"),
        (0.5, "ok"),
        (0.79, "ok"),
        (0.80, "warn"),
        (0.89, "warn"),
        (0.90, "over"),
        (1.5, "over"),
    ],
)
def test_budget_state_is_decided_by_the_thresholds(monkeypatch, fraction, expected):
    """The watchdog raises on anything that is not ok, so the boundaries matter.

    Tested at the boundaries rather than in the middle of each band: an
    off-by-one on `>=` is the whole of what could be wrong here, and a test at
    0.5 and 0.95 would not see it.
    """
    from bellwether import config, usage

    config.get_settings.cache_clear()
    budget = usage.FREE_TIER_BUDGET_BYTES
    monkeypatch.setattr(usage, "period_total", lambda: (int(budget * fraction), 0, 1))
    try:
        assert usage.budget_status()["state"] == expected
    finally:
        config.get_settings.cache_clear()


def test_budget_status_reports_the_estimate_as_an_estimate(monkeypatch):
    """The caveat travels with the number, wherever the number is shown.

    The figure is published on an operational surface and quoted in a watchdog
    alert. Someone will eventually compare it to the provider's console and
    find it disagrees; the payload has to have said so first.
    """
    from bellwether import config, usage

    config.get_settings.cache_clear()
    monkeypatch.setattr(usage, "period_total", lambda: (1024, 10, 1))
    try:
        note = usage.budget_status()["estimate_note"].lower()
    finally:
        config.get_settings.cache_clear()

    assert "estimated" in note
    assert "not measured on the wire" in note


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def test_human_bytes_scales():
    assert human_bytes(512).endswith("B")
    assert "KB" in human_bytes(2048)
    assert "MB" in human_bytes(5 * 1024**2)
    assert "GB" in human_bytes(3 * 1024**3)


def test_human_bytes_does_not_run_out_of_units():
    """An oversized figure must render rather than raise."""
    assert "GB" in human_bytes(9 * 1024**5)
