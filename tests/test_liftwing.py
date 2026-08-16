"""The institutional benchmark (M4-FR-18 to FR-21).

Mostly about what this must refuse to do: impute a missing score, substitute a
reachable comparator for a gated one, or set two different populations against
each other and call the difference a margin.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
def test_a_deleted_revision_is_absent_rather_than_the_end_of_the_run() -> None:
    """Lift Wing says `revision_info_deleted` with a 422, not a 404.

    That one status code was the difference between "they declined to score
    this one" and an UpstreamError that ended the whole job. It killed two
    scheduled runs, and because the crash escaped before the attempt row was
    written, /metrics went on showing the last SUCCESSFUL fetch — `191 of 191
    (ok)` — for two days beside a job that had not run since.

    Fetching newest first is what makes this the common case rather than a
    curiosity: the newest revisions are the ones still liable to be deleted.
    """
    route = respx.post(liftwing.ENDPOINT).mock(
        return_value=httpx.Response(
            422,
            json={
                "detail": (
                    "Could not make prediction for revisions "
                    "dict_keys([(1369460563, 'en')]). Reason: ['revision_info_deleted']"
                )
            },
        )
    )
    with httpx.Client() as client:
        assert liftwing.score_one(client, _limiter(), 1369460563) is None
    assert route.call_count == 1, "a deleted revision stays deleted; retrying spends their budget"


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


def _sampled_revids(n: int) -> list[int]:
    """Revids the published 10% bucket actually selects.

    Found rather than hard-coded: the salt is a constant of the sampling rule,
    and a test carrying its own copy of the answers would keep passing after
    someone changed it.
    """
    found: list[int] = []
    revid = 1_300_000_000
    while len(found) < n:
        if liftwing.sampled(revid):
            found.append(revid)
        revid += 1
    return found


def _scores_kept(conn: Any) -> int:
    row = conn.execute("SELECT count(*) AS n FROM outcome.liftwing_scores").fetchone()
    assert row is not None
    return int(row["n"])


def _register(conn: Any, revids: list[int]) -> None:
    for i, revid in enumerate(revids):
        conn.execute(
            """
            INSERT INTO register.predictions
                (revid, event_ts, scored_at, model_version, role, score, feature_hash)
            VALUES (%(revid)s, %(ts)s, %(ts)s, 'v1', 'champion', 0.42, 'abc123')
            """,
            {"revid": revid, "ts": datetime(2026, 8, 14, 12, i, tzinfo=UTC)},
        )


@pytest.mark.db
@respx.mock
def test_one_deleted_revision_does_not_cost_the_rest_of_the_batch(
    fresh_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scores either side of it were already paid for.

    Before this, a single 422 partway through discarded every score the run had
    collected and left no record that it had tried.
    """
    monkeypatch.setattr(liftwing, "REQUESTS_PER_MINUTE", 600)
    revids = _sampled_revids(3)
    with connect() as conn:
        _register(conn, revids)

    scored = httpx.Response(200, json={"output": {"probabilities": {"true": 0.3}}})
    respx.post(liftwing.ENDPOINT).side_effect = [
        scored,
        httpx.Response(422, json={"detail": "Reason: ['revision_info_deleted']"}),
        scored,
    ]

    result = liftwing.run(limit=3)

    assert result["fetched"] == 2, "the two they scored are kept"
    assert result["status"] == "partial", "and the one they declined is visible as a shortfall"

    with connect() as conn:
        kept = _scores_kept(conn)
        attempt = conn.execute(
            "SELECT requested, fetched, status FROM outcome.liftwing_attempts"
        ).fetchone()
    assert kept == 2
    assert attempt is not None
    assert (attempt["requested"], attempt["fetched"], attempt["status"]) == (3, 2, "partial")


@pytest.mark.db
@respx.mock
def test_an_unexpected_failure_is_recorded_before_it_is_raised(
    fresh_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure that matters is the one nobody anticipated.

    A run that dies without writing an attempt row leaves /metrics serving the
    last successful one, so a dead job reads as a healthy one — which is the
    single worst way for this particular project to fail. The row is written
    first; the run still goes red afterwards.
    """
    monkeypatch.setattr(liftwing, "REQUESTS_PER_MINUTE", 600)
    revids = _sampled_revids(3)
    with connect() as conn:
        _register(conn, revids)

    class Boom(Exception):
        """Deliberately not Gated, UpstreamError, or an httpx error."""

    calls = {"n": 0}
    real = liftwing.score_one

    def explode_on_the_third(*args: Any, **kw: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 3:
            raise Boom("the database went away")
        return real(*args, **kw)

    monkeypatch.setattr(liftwing, "score_one", explode_on_the_third)
    respx.post(liftwing.ENDPOINT).mock(
        return_value=httpx.Response(200, json={"output": {"probabilities": {"true": 0.3}}})
    )

    with pytest.raises(Boom):
        liftwing.run(limit=3)

    with connect() as conn:
        attempt = conn.execute(
            "SELECT requested, fetched, status, detail FROM outcome.liftwing_attempts"
        ).fetchone()
        kept = _scores_kept(conn)

    assert attempt is not None, "the attempt is recorded even though the run died"
    assert attempt["status"] == "unavailable"
    assert "Boom" in (attempt["detail"] or "")
    assert kept == 2, "and the scores collected before it are committed, not rolled back"


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
