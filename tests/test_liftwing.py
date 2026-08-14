"""The institutional benchmark (M4-FR-18 to FR-21).

Mostly about what this must refuse to do: impute a missing score, substitute a
reachable comparator for a gated one, or set two different populations against
each other and call the difference a margin.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from bellwether import liftwing, metrics
from bellwether.db import connect


def test_the_module_imports() -> None:
    assert liftwing.MODEL_NAME == "revertrisk-language-agnostic"


def _limiter() -> Any:
    return liftwing.RateLimiter(600)  # 100ms apart; the tests are not measuring politeness


@respx.mock
def test_a_score_is_read_from_the_probability_of_true() -> None:
    respx.post(liftwing.ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "model_name": "revertrisk-language-agnostic",
                "model_version": "3",
                "output": {"prediction": True, "probabilities": {"true": 0.82, "false": 0.18}},
            },
        )
    )
    with httpx.Client() as client:
        result = liftwing.score_one(client, _limiter(), 123)
    assert result is not None
    assert result["score"] == pytest.approx(0.82)
    assert result["model_version"] == "3"


@respx.mock
def test_a_revision_they_will_not_score_is_absent_not_zero() -> None:
    """A missing score imputed as 0.0 would make their model look wrong about an
    edit it never saw — and would do so in the direction that flatters this
    project, which is the direction to be most careful about."""
    respx.post(liftwing.ENDPOINT).mock(return_value=httpx.Response(404, json={"detail": "gone"}))
    with httpx.Client() as client:
        assert liftwing.score_one(client, _limiter(), 123) is None


@respx.mock
def test_a_response_without_a_probability_is_absent_too() -> None:
    respx.post(liftwing.ENDPOINT).mock(return_value=httpx.Response(200, json={"output": {}}))
    with httpx.Client() as client:
        assert liftwing.score_one(client, _limiter(), 123) is None


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_a_gated_service_raises_rather_than_returning_nothing(status: int) -> None:
    """M4-FR-21. Silently returning None would look identical to "Wikimedia had
    no opinion about these edits", and the benchmark would quietly become an
    empty set nobody questioned."""
    respx.post(liftwing.ENDPOINT).mock(return_value=httpx.Response(status, text="needs a token"))
    with httpx.Client() as client, pytest.raises(liftwing.Gated):
        liftwing.score_one(client, _limiter(), 123)


# --- the comparison ---------------------------------------------------------


def _paired(n: int, *, positives: int, theirs_better: bool) -> list[dict[str, Any]]:
    """Lift Wing separates the classes cleanly; ours gets one positive in three
    wrong when `theirs_better`.

    The first version gave both scorers a perfect ranking and then asserted the
    margin was negative. It was 0.0, correctly — two perfect scorers tie. A
    fixture has to make the thing it claims to measure actually happen.
    """
    rows = []
    for i in range(n):
        label = i < positives
        ours = 0.9 if label else 0.1
        if theirs_better and label and i % 3 == 0:
            ours = 0.05  # confidently wrong, where theirs is right
        rows.append(
            {
                "label": label,
                "score": ours,
                "liftwing_score": 0.95 if label else 0.05,
                "sampling_weight": 1.0,
                "is_logged_out": label,
            }
        )
    return rows


def test_too_few_paired_events_publish_a_count_and_no_margin() -> None:
    """A margin over twelve events is a number that should not be read, and
    publishing it with a wide interval beside it is not enough — someone will
    quote the point estimate."""
    result = metrics.liftwing_comparison(_paired(12, positives=4, theirs_better=True))
    assert result["liftwing_n"] == 12
    assert result["liftwing_margin"] is None


def test_the_margin_is_computed_only_on_events_both_models_scored() -> None:
    """Lift Wing is sampled. Setting its PR-AUC over a few hundred events
    against this project's over the whole window would be two populations
    dressed as a margin, which is why model_pr_auc_on_paired exists and is not
    the same field as pr_auc."""
    rows = _paired(100, positives=25, theirs_better=True)
    # Sampled: a spread of events was never sent, positives among them. Nulling
    # a prefix instead would have removed every positive and left a subset with
    # one class, which is a different thing being tested by accident.
    for i, row in enumerate(rows):
        if i % 2:
            row["liftwing_score"] = None

    result = metrics.liftwing_comparison(rows)
    assert result["liftwing_n"] == 50
    assert result["model_pr_auc_on_paired"] is not None


def test_a_losing_margin_is_reported_as_readily_as_a_winning_one() -> None:
    """SRS 6.4 predicted Wikimedia's model wins. The benchmark is worth nothing
    if it only gets published when it does not."""
    result = metrics.liftwing_comparison(_paired(200, positives=40, theirs_better=True))
    assert result["liftwing_margin"] is not None
    assert result["liftwing_margin"] < 0, "theirs is better here, and the sign must say so"
    assert result["liftwing_margin_ci_low"] <= result["liftwing_margin"]


@pytest.mark.db
def test_an_attempt_is_recorded_even_when_nothing_was_fetched(fresh_db: None) -> None:
    """M4-FR-21 again, at the run level. A gated service must leave a row
    saying so; an absent comparison with no explanation is indistinguishable
    from one nobody tried."""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO outcome.liftwing_attempts (requested, fetched, status, detail)
            VALUES (200, 0, 'gated', '403 from Lift Wing')
            """
        )
        row = conn.execute(
            "SELECT status, fetched FROM outcome.liftwing_attempts ORDER BY attempted_at DESC"
        ).fetchone()
    assert row is not None and row["status"] == "gated" and row["fetched"] == 0


def test_the_sample_is_deterministic_and_close_to_the_published_rate() -> None:
    """M4-FR-25. The rate is a stated constant, not whatever a batch size
    implied — otherwise "sampled at 10%" is a claim nobody can check."""
    ids = range(1_300_000_000, 1_300_020_000)
    once = [r for r in ids if liftwing.sampled(r)]
    assert once == [r for r in ids if liftwing.sampled(r)]
    assert 0.08 < len(once) / 20_000 < 0.12


@pytest.mark.db
def test_a_run_with_nothing_to_do_still_records_that_it_ran(fresh_db: None) -> None:
    """The first production run returned early with no rows in the register old
    enough to fetch, and wrote nothing. /metrics then showed no attempt at all,
    which is indistinguishable from nobody having run the job — and the one
    thing worth learning early was whether the endpoint answers.
    """
    result = liftwing.run(limit=10)
    assert result["requested"] == 0

    with connect() as conn:
        row = conn.execute(
            "SELECT requested, fetched, status, detail FROM outcome.liftwing_attempts"
        ).fetchone()
    assert row is not None
    assert row["requested"] == 0
    assert row["status"] == "ok"
    assert "no unscored" in (row["detail"] or "").lower()


@respx.mock
def test_a_transient_5xx_is_retried_rather_than_ending_the_run() -> None:
    """The first production run fetched 55 of 200 and stopped.

    A single 503 partway through was reported as the service being
    unavailable, when it was a blip on someone else's server. Every other
    upstream call in this project retries 5xx; this one did not.
    """
    route = respx.post(liftwing.ENDPOINT)
    route.side_effect = [
        httpx.Response(503, text="try later"),
        httpx.Response(
            200, json={"output": {"probabilities": {"true": 0.4}}, "model_version": "3"}
        ),
    ]
    with httpx.Client() as client:
        result = liftwing.score_one(client, _limiter(), 123)
    assert result is not None
    assert result["score"] == pytest.approx(0.4)
    assert route.call_count == 2


@respx.mock
def test_a_persistent_5xx_gives_up_after_its_retries() -> None:
    respx.post(liftwing.ENDPOINT).mock(return_value=httpx.Response(503, text="down"))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        liftwing.score_one(client, _limiter(), 123)


@respx.mock
def test_a_malformed_request_is_not_retried() -> None:
    """A 400 stays a 400 however often it is sent, and retrying only spends
    someone else's bandwidth."""
    route = respx.post(liftwing.ENDPOINT).mock(return_value=httpx.Response(400, text="bad"))
    with httpx.Client() as client, pytest.raises(liftwing.UpstreamError):
        liftwing.score_one(client, _limiter(), 123)
    assert route.call_count == 1
