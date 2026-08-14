"""The label-quality study (M7 §5, BQ-8).

The tests that matter are about what this refuses to say. Computing κ is
arithmetic; refusing to publish one over thirty events judged by one person, on
a slice the model chose, is the part that takes deliberate effort.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from bellwether import agreement
from bellwether.db import connect


def _label(
    verdict: str, *, reverted: bool, queue_slice: str = "random", reviewer: str = "r1"
) -> dict[str, Any]:
    return {
        "revid": 0,
        "queue_slice": queue_slice,
        "verdict": verdict,
        "reviewer": reviewer,
        "proxy_reverted": reverted,
    }


def _agreeing(n: int, **kwargs: Any) -> list[dict[str, Any]]:
    """Perfect agreement, half positive."""
    return [
        _label("bad_edit" if i < n // 2 else "good_edit", reverted=i < n // 2, **kwargs)
        for i in range(n)
    ]


def test_perfect_agreement_is_kappa_one() -> None:
    counts = agreement.confusion(_agreeing(200), unsure_policy="excluded")
    kappa, po, _ = agreement.cohens_kappa(**counts)
    assert kappa == pytest.approx(1.0)
    assert po == pytest.approx(1.0)


def test_chance_agreement_is_kappa_zero() -> None:
    """Half the events reverted, a human calling bad at random and independently
    — they agree half the time, which is exactly chance."""
    rows = []
    for i in range(400):
        rows.append(_label("bad_edit" if i % 2 == 0 else "good_edit", reverted=(i // 2) % 2 == 0))
    counts = agreement.confusion(rows, unsure_policy="excluded")
    kappa, _, _ = agreement.cohens_kappa(**counts)
    assert kappa == pytest.approx(0.0, abs=0.05)


def test_kappa_is_undefined_rather_than_zero_when_one_rater_never_varies() -> None:
    """Not a κ of zero, which reads as "no better than chance". There is no
    chance model to compare against when one rater put everything in one class,
    and reporting a number would be answering a question the data cannot."""
    rows = [_label("good_edit", reverted=False) for _ in range(200)]
    counts = agreement.confusion(rows, unsure_policy="excluded")
    kappa, po, pe = agreement.cohens_kappa(**counts)

    assert kappa is None
    assert po == pytest.approx(1.0)
    assert pe == pytest.approx(1.0)


def test_no_kappa_below_the_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """M7-FR-14. A κ over thirty events is a number with a Greek letter on it,
    and it would be quoted."""
    result = agreement.study(_agreeing(40), queue_slice="random", unsure_policy="excluded")

    assert result["n"] == 40
    assert result["kappa"] is None
    assert "100 required" in result["refused"]


def test_one_reviewer_is_not_a_measurement_of_the_proxy() -> None:
    """M7-FR-16. With one rater there is no way to tell how much of a
    disagreement is the proxy being wrong and how much is that person."""
    result = agreement.study(_agreeing(200), queue_slice="random", unsure_policy="excluded")

    assert result["kappa"] is not None, "the figure is computed"
    assert result["reviewers"] == 1
    assert "one person" in result["refused"], "and labelled as one person's judgement"


def test_two_reviewers_lift_the_caveat() -> None:
    rows = _agreeing(100, reviewer="r1") + _agreeing(100, reviewer="r2")
    result = agreement.study(rows, queue_slice="random", unsure_policy="excluded")

    assert result["reviewers"] == 2
    assert result["refused"] is None


def test_the_random_slice_is_computed_separately_from_the_ranked_one() -> None:
    """M7-FR-4, the reason the slice exists at all. A κ over the ranked slice
    measures agreement among edits the model already flagged."""
    rows = _agreeing(200, queue_slice="random") + _agreeing(60, queue_slice="ranked")

    random_only = agreement.study(rows, queue_slice="random", unsure_policy="excluded")
    ranked_only = agreement.study(rows, queue_slice="ranked", unsure_policy="excluded")
    pooled = agreement.study(rows, queue_slice="all", unsure_policy="excluded")

    assert random_only["n"] == 200
    assert ranked_only["n"] == 60
    assert pooled["n"] == 260
    assert ranked_only["kappa"] is None, "60 is below the threshold"


def test_unsure_is_never_silently_dropped() -> None:
    """M7-FR-13. Those are the ambiguous cases, where the proxy is most likely
    to disagree with a human — dropping them selects on the outcome being
    studied."""
    rows = _agreeing(180) + [_label("unsure", reverted=True) for _ in range(20)]

    excluded = agreement.study(rows, queue_slice="random", unsure_policy="excluded")
    as_good = agreement.study(rows, queue_slice="random", unsure_policy="as_good")

    assert excluded["n_unsure"] == 20
    assert excluded["unsure_rate"] == pytest.approx(0.1)
    assert excluded["n"] == 180, "excluded from the matrix"
    assert as_good["n"] == 200, "and counted as good under the other policy"

    # The two treatments are different estimates of different things, and the
    # twenty unsure events were all reverted — so counting them as "good" moves
    # the figure.
    assert excluded["kappa"] != as_good["kappa"]


def test_the_two_policies_are_both_computed_and_labelled() -> None:
    assert set(agreement.UNSURE_POLICIES) == {"excluded", "as_good"}


@pytest.mark.db
def test_a_run_with_no_labels_records_that_it_ran(fresh_db: None) -> None:
    """A refusal is a measurement. "We have not measured this" and "we measured
    it and there was not enough" are different states, and an empty table cannot
    tell them apart."""
    result = agreement.run()
    assert result["n"] == 0

    with connect() as conn:
        rows = conn.execute(
            "SELECT queue_slice, unsure_policy, n, kappa, refused_reason "
            "FROM outcome.label_agreement ORDER BY queue_slice, unsure_policy"
        ).fetchall()

    # Three slices times two unsure policies, every one recorded.
    assert len(rows) == 6
    assert all(row["kappa"] is None for row in rows)
    assert all("required before a kappa" in row["refused_reason"] for row in rows)


@pytest.mark.db
def test_only_matured_labels_enter_the_study(fresh_db: None) -> None:
    """A human label on an edit whose outcome is not settled has nothing to be
    compared against, and including it would count "nobody has checked" as "not
    reverted"."""
    with connect() as conn:
        user_id = conn.execute(
            "INSERT INTO app.users (email, password_hash, password_salt, kdf_params, role) "
            "VALUES ('r@example.test', 'x', 'y', '{}'::jsonb, 'reviewer') RETURNING user_id"
        ).fetchone()["user_id"]

        for revid, hours_ago, checked in ((1, 240, True), (2, 2, False)):
            conn.execute(
                "INSERT INTO landing.rc_events "
                "(revid, event_ts, ns, title, is_anon, is_temp, is_minor, is_bot, "
                " sampling_stratum, sampling_weight, ingested_at_utc) "
                "VALUES (%s, now() - make_interval(hours => %s), 0, 'P', false, false, "
                "        false, false, 'registered', 33.3, now())",
                (revid, hours_ago),
            )
            if checked:
                conn.execute(
                    "INSERT INTO outcome.label_checks "
                    "(revid, checkpoint_seconds, checked_at_utc, age_seconds, had_reverted_tag) "
                    "VALUES (%s, %s, now(), %s, true)",
                    (revid, 7 * 24 * 3600, 9 * 24 * 3600),
                )
            conn.execute(
                "INSERT INTO app.human_labels (revid, user_id, verdict, confidence, queue_slice) "
                "VALUES (%s, %s, 'bad_edit', 'high', 'random')",
                (revid, user_id),
            )

        rows = conn.execute(
            "SELECT * FROM outcome.labels_for_agreement(%s)", (7 * 24 * 3600,)
        ).fetchall()

    assert [row["revid"] for row in rows] == [1]


@pytest.mark.db
def test_the_study_never_returns_who_judged_what(fresh_db: None) -> None:
    """M7-FR-15 publishes a reviewer COUNT. The door into the labels returns a
    uuid so distinct raters can be counted, and nothing downstream carries it —
    a public agreement figure that identified a reviewer would publish one
    person's opinions about named strangers' edits."""
    result = agreement.study(
        _agreeing(200, reviewer=str(uuid.uuid4())),
        queue_slice="random",
        unsure_policy="excluded",
    )
    assert "reviewers" in result
    assert "reviewer" not in result
    assert isinstance(result["reviewers"], int)


# --- human labels in training (SRS FR-48, M7-FR-6 to FR-10) -----------------


def test_the_training_weight_is_fixed_in_code_not_derived() -> None:
    """M7-FR-7. Deriving it would mean trying several and keeping the one that
    scored best — a hyperparameter selected on the evaluation it is about to be
    judged by.

    Asserted as a property of the assignment rather than by searching the file
    for a substring: the first version of this test matched the module's own
    variable names and failed on the code it was inspecting.
    """
    import inspect

    from bellwether import train

    assert train.HUMAN_LABEL_WEIGHT == 3.0

    lines = [
        line.strip()
        for line in inspect.getsource(train).splitlines()
        if "HUMAN_LABEL_WEIGHT" in line and "=" in line and not line.strip().startswith("#")
    ]
    assignments = [line for line in lines if line.startswith("HUMAN_LABEL_WEIGHT")]

    assert assignments == ["HUMAN_LABEL_WEIGHT = 3.0"], (
        "the weight must be a literal module constant, not read from a setting, "
        "an argument or the environment"
    )


@pytest.mark.db
def test_a_human_label_adds_a_row_rather_than_overwriting_the_proxy(fresh_db: None) -> None:
    """M7-FR-6 and SRS FR-47. An edit a reviewer called bad but nobody reverted
    appears twice: the proxy's answer at weight 1, the human's at weight 3.

    Overwriting would make every published figure unverifiable, because nobody
    could tell afterwards which rows came from where.
    """
    with connect() as conn:
        user_id = conn.execute(
            "INSERT INTO app.users (email, password_hash, password_salt, kdf_params, role) "
            "VALUES ('t@example.test', 'x', 'y', '{}'::jsonb, 'reviewer') RETURNING user_id"
        ).fetchone()["user_id"]
        conn.execute(
            "INSERT INTO landing.rc_events "
            "(revid, event_ts, ns, title, is_anon, is_temp, is_minor, is_bot, "
            " sampling_stratum, sampling_weight, ingested_at_utc) "
            "VALUES (1, now() - interval '20 days', 0, 'P', false, false, false, false, "
            "        'registered', 33.3, now())"
        )
        conn.execute(
            "INSERT INTO app.human_labels (revid, user_id, verdict, confidence, queue_slice) "
            "VALUES (1, %s, 'bad_edit', 'high', 'random')",
            (user_id,),
        )
        # The proxy says the opposite: nobody reverted it.
        rows = conn.execute(
            "SELECT * FROM app.labels_for_training(now() - interval '30 days', now())"
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["verdict"] == "bad_edit"
    assert rows[0]["queue_slice"] == "random"


@pytest.mark.db
def test_unsure_contributes_no_training_row(fresh_db: None) -> None:
    """It is a reviewer declining to answer, not a third class and not a soft
    label. Turning it into a target would invent an opinion."""
    with connect() as conn:
        user_id = conn.execute(
            "INSERT INTO app.users (email, password_hash, password_salt, kdf_params, role) "
            "VALUES ('u@example.test', 'x', 'y', '{}'::jsonb, 'reviewer') RETURNING user_id"
        ).fetchone()["user_id"]
        for revid, verdict in ((1, "unsure"), (2, "good_edit")):
            conn.execute(
                "INSERT INTO landing.rc_events "
                "(revid, event_ts, ns, title, is_anon, is_temp, is_minor, is_bot, "
                " sampling_stratum, sampling_weight, ingested_at_utc) "
                "VALUES (%s, now() - interval '20 days', 0, 'P', false, false, false, false, "
                "        'registered', 33.3, now())",
                (revid,),
            )
            conn.execute(
                "INSERT INTO app.human_labels (revid, user_id, verdict, confidence) "
                "VALUES (%s, %s, %s, 'low')",
                (revid, user_id, verdict),
            )
        rows = conn.execute(
            "SELECT * FROM app.labels_for_training(now() - interval '30 days', now())"
        ).fetchall()

    assert [row["revid"] for row in rows] == [2]


@pytest.mark.db
def test_the_training_door_never_returns_who_judged(fresh_db: None) -> None:
    """The reviewer's identity must not reach the model or its registry entry.
    A weight is a fact about a label; who wrote it is not."""
    with connect() as conn:
        columns = conn.execute(
            "SELECT proargnames FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'app' AND p.proname = 'labels_for_training'"
        ).fetchone()
    assert "user_id" not in (columns["proargnames"] or [])
    assert "reviewer" not in (columns["proargnames"] or [])
