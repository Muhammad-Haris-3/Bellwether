"""The queue and human labels (M6 §5, §6).

The tests worth having are about what the queue refuses to imply. A queue that
returns rows is easy; one that never lets an unmatured score read as a verdict
takes deliberate effort, and none of it shows up in a happy path.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Json

from api import sessions
from api.main import app
from bellwether import auth
from bellwether.config import get_settings
from bellwether.db import connect

APP_PASSWORD = "review-test-password"  # noqa: S105 - local test role, never deployed
USER_PASSWORD = "reviewer-password"  # noqa: S105


def _make_user(conn: Any, email: str, role: str) -> Any:
    digest, salt, params = auth.hash_password(USER_PASSWORD)
    return conn.execute(
        "INSERT INTO app.users (email, password_hash, password_salt, kdf_params, role) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING user_id",
        (email, digest, salt, Json(params), role),
    ).fetchone()["user_id"]


def _event(conn: Any, revid: int, *, hours_ago: int, score: float, title: str = "Page") -> None:
    conn.execute(
        "INSERT INTO landing.rc_events "
        "(revid, event_ts, ns, title, user_name, user_id, is_anon, is_temp, is_minor, "
        " is_bot, oldlen, newlen, comment, tags, sampling_stratum, sampling_weight, "
        " ingested_at_utc) "
        "VALUES (%s, now() - make_interval(hours => %s), 0, %s, 'Alice', 5, false, false, "
        "        false, false, 100, 140, 'an edit', '{}', 'registered', 33.3, now())",
        (revid, hours_ago, title),
    )
    conn.execute(
        "INSERT INTO register.predictions "
        "(revid, event_ts, scored_at, model_version, role, score, feature_hash, "
        " outcome_observable_at_scoring) "
        "SELECT revid, event_ts, event_ts + interval '5 minutes', 'champ', 'champion', "
        "       %s, 'h', false FROM landing.rc_events WHERE revid = %s",
        (score, revid),
    )


@pytest.fixture
def world(fresh_db: None, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    url = os.environ.get("BELLWETHER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("BELLWETHER_TEST_DATABASE_URL is not set")

    with connect() as conn:
        ids = {
            role: _make_user(conn, f"{role}@example.test", role)
            for role in ("viewer", "reviewer", "admin")
        }
        conn.execute(
            "INSERT INTO register.model_registry "
            "(model_version, training_start, training_end, n_train_events, n_train_positives, "
            " feature_names, hyperparameters, offline_metrics, artifact_path, artifact_sha256) "
            "VALUES ('champ', now() - interval '40 days', now() - interval '39 days', 100, 10, "
            "        ARRAY['a'], '{}'::jsonb, '{}'::jsonb, 'models/x.pkl', %s)",
            ("a" * 64,),
        )
        conn.execute("INSERT INTO decide.champion_history (model_version) VALUES ('champ')")

        # One fresh edit nobody has checked, one old and checked.
        _event(conn, 1, hours_ago=2, score=0.92, title="Fresh")
        _event(conn, 2, hours_ago=240, score=0.31, title="Old")
        conn.execute(
            "INSERT INTO outcome.label_checks "
            "(revid, checkpoint_seconds, checked_at_utc, age_seconds, had_reverted_tag) "
            "VALUES (2, %s, now(), %s, true)",
            (7 * 24 * 3600, 9 * 24 * 3600),
        )
        conn.execute(
            "INSERT INTO outcome.labels "
            "(revid, label, label_source, first_observed_at_utc, detection_latency_seconds) "
            "VALUES (2, true, 'mw_reverted', now(), 900)"
        )
        conn.execute("ALTER ROLE bellwether_app WITH LOGIN PASSWORD " + f"'{APP_PASSWORD}'")

    tail = url.split("@", 1)[1]
    monkeypatch.setenv(
        "BELLWETHER_APP_DATABASE_URL", f"postgresql://bellwether_app:{APP_PASSWORD}@{tail}"
    )
    get_settings.cache_clear()
    sessions._attempts.clear()
    return ids


def _signed_in(role: str) -> TestClient:
    client = TestClient(app)
    response = client.post(
        "/auth/sign-in", json={"email": f"{role}@example.test", "password": USER_PASSWORD}
    )
    assert response.status_code == 200, response.text
    client.headers[sessions.CSRF_HEADER] = response.json()["csrf_token"]
    return client


@pytest.mark.db
def test_the_queue_needs_a_session(world: dict[str, Any]) -> None:
    assert TestClient(app).get("/queue").status_code == 401


@pytest.mark.db
def test_an_unmatured_item_reports_no_outcome_at_all(world: dict[str, Any]) -> None:
    """FR-41, and the single easiest way this project could mislead somebody.

    `reverted: false` on a two-hour-old edit would read as "this edit survived".
    It has not survived anything yet — nobody has looked.
    """
    body = _signed_in("viewer").get("/queue").json()
    fresh = next(i for i in body["items"] if i["title"] == "Fresh")

    assert fresh["matured"] is False
    assert fresh["reverted"] is None


@pytest.mark.db
def test_a_matured_item_reports_its_outcome(world: dict[str, Any]) -> None:
    # 336 hours, because a matured item is by definition older than the maturity
    # window and cannot appear in a one-day queue.
    body = _signed_in("viewer").get("/queue", params={"hours": 336}).json()
    old = next(i for i in body["items"] if i["title"] == "Old")

    assert old["matured"] is True
    assert old["reverted"] is True


@pytest.mark.db
def test_the_queue_counts_its_own_maturity_and_publishes_the_window(
    world: dict[str, Any],
) -> None:
    """The reviewer should be able to see, without arithmetic, that most of what
    they are looking at has no outcome yet."""
    body = _signed_in("viewer").get("/queue", params={"hours": 336}).json()

    assert body["matured"] + body["immature"] == len(body["items"])
    assert body["maturity_hours"] == 168
    assert "immature" in body["note"]


@pytest.mark.db
def test_the_queue_carries_no_accuracy_figure(world: dict[str, Any]) -> None:
    """M6-FR-17. Precision over a queue of unmatured items is not a
    low-confidence estimate, it is a category error."""
    body = _signed_in("viewer").get("/queue").json()

    # The DATA, not the prose. The note says "no accuracy figure is computed
    # over this list", which contains the word — a substring search over the
    # whole payload would fail on the sentence explaining the property it is
    # checking for.
    payload = {k: v for k, v in body.items() if k != "note"}
    for forbidden in ("precision", "pr_auc", "accuracy", "recall"):
        assert forbidden not in str(payload).lower()


@pytest.mark.db
def test_the_page_is_selected_by_score_even_though_it_is_not_ordered_by_it(
    world: dict[str, Any],
) -> None:
    """FR-40 as amended 2026-08-14.

    The CONTENTS are chosen by rank; the display order is shuffled so a
    reviewer cannot tell a randomly drawn row from where it sits. Both halves
    matter: selection is what makes this triage, shuffling is what makes the
    random slice a control.
    """
    with connect() as conn:
        for revid in range(10, 40):
            _event(conn, revid, hours_ago=3, score=0.01 * (revid - 9), title=f"P{revid}")
        _event(conn, 99, hours_ago=3, score=0.999, title="Highest")

    body = _signed_in("viewer").get("/queue", params={"limit": 5}).json()
    revids = {i["revid"] for i in body["items"]}

    # The highest-scoring edit is in the page. With a purely random draw over
    # thirty events it would usually not be.
    assert 99 in revids


@pytest.mark.db
def test_the_score_is_withheld_until_a_verdict_is_recorded(world: dict[str, Any]) -> None:
    """M7 §2. A reviewer shown 0.92 is agreeing or disagreeing with a number,
    not forming an opinion about the edit — and BQ-8 asks what a human thinks
    the edit was.

    Withheld server-side rather than hidden in the page: a score returned and
    not displayed is a secret anyone can read with the network tab open.
    """
    client = _signed_in("reviewer")
    before = client.get("/queue", params={"hours": 336}).json()
    assert all(i["score"] is None for i in before["items"])
    assert all(i["model_version"] is None for i in before["items"])

    revealed = client.post(
        "/labels", json={"revid": 1, "verdict": "bad_edit", "confidence": "high"}
    ).json()
    assert revealed["score"] == pytest.approx(0.92), "revealed once the answer is committed"

    after = client.get("/queue", params={"hours": 336}).json()
    judged = next(i for i in after["items"] if i["revid"] == 1)
    assert judged["score"] == pytest.approx(0.92)


@pytest.mark.db
def test_a_label_records_the_slice_the_server_offered_it_in(world: dict[str, Any]) -> None:
    """M7-FR-2. From the server's own selection, never the client's claim — a
    slice a caller could assert is a slice a caller could choose, and the random
    slice is the only estimate that answers BQ-8."""
    client = _signed_in("reviewer")
    client.get("/queue", params={"hours": 336})
    client.post("/labels", json={"revid": 1, "verdict": "bad_edit", "confidence": "high"})

    with connect() as conn:
        row = conn.execute(
            "SELECT queue_slice, score_was_visible FROM app.human_labels WHERE revid = 1"
        ).fetchone()

    assert row["queue_slice"] in ("ranked", "random")
    assert row["score_was_visible"] is False


@pytest.mark.db
def test_a_judgement_on_a_revision_never_offered_defaults_to_ranked(
    world: dict[str, Any],
) -> None:
    """The conservative direction. A random-slice label mistakenly recorded as
    ranked is dropped from the study; the reverse would contaminate the one
    estimate that answers BQ-8."""
    client = _signed_in("reviewer")
    # No queue fetch first, so the server has no memory of offering this row.
    client.post("/labels", json={"revid": 1, "verdict": "good_edit", "confidence": "low"})

    with connect() as conn:
        row = conn.execute("SELECT queue_slice FROM app.human_labels WHERE revid = 1").fetchone()
    assert row["queue_slice"] == "ranked"


@pytest.mark.db
def test_a_viewer_cannot_record_a_judgement(world: dict[str, Any]) -> None:
    client = _signed_in("viewer")
    response = client.post(
        "/labels", json={"revid": 1, "verdict": "bad_edit", "confidence": "high"}
    )
    assert response.status_code == 403

    # And the refusal is recorded, which is what shows the control working
    # rather than merely present.
    with connect() as conn:
        row = conn.execute(
            "SELECT action, outcome FROM app.audit_log ORDER BY audit_id DESC LIMIT 1"
        ).fetchone()
    assert row["outcome"] == "refused"


@pytest.mark.db
def test_a_reviewer_records_the_score_they_were_shown(world: dict[str, Any]) -> None:
    """M6-FR-20. A judgement is partly a reaction to the number on the screen,
    and by M7 that is unrecoverable unless it is captured now."""
    client = _signed_in("reviewer")
    response = client.post(
        "/labels", json={"revid": 1, "verdict": "bad_edit", "confidence": "high"}
    )
    assert response.status_code == 200
    assert response.json()["recorded"] is True

    with connect() as conn:
        row = conn.execute(
            "SELECT verdict, confidence, champion_version, score_shown, was_matured "
            "FROM app.human_labels WHERE revid = 1"
        ).fetchone()

    assert row["verdict"] == "bad_edit"
    assert row["champion_version"] == "champ"
    assert float(row["score_shown"]) == pytest.approx(0.92)
    assert row["was_matured"] is False, "judged before the outcome existed, and recorded as such"


@pytest.mark.db
def test_a_second_judgement_by_the_same_reviewer_is_not_a_second_opinion(
    world: dict[str, Any],
) -> None:
    """A double-submitted form or a second tab. Not an error, and not a second
    judgement either."""
    client = _signed_in("reviewer")
    payload = {"revid": 1, "verdict": "bad_edit", "confidence": "high"}
    client.post("/labels", json=payload)
    second = client.post("/labels", json={**payload, "verdict": "good_edit"})

    assert second.status_code == 200
    assert second.json()["already_judged"] is True

    with connect() as conn:
        rows = conn.execute("SELECT verdict FROM app.human_labels WHERE revid = 1").fetchall()
    assert [r["verdict"] for r in rows] == ["bad_edit"], "the first judgement stands"


@pytest.mark.db
def test_a_judgement_without_the_csrf_header_is_refused(world: dict[str, Any]) -> None:
    client = _signed_in("reviewer")
    del client.headers[sessions.CSRF_HEADER]
    response = client.post(
        "/labels", json={"revid": 1, "verdict": "bad_edit", "confidence": "high"}
    )
    assert response.status_code == 403


@pytest.mark.db
def test_a_judgement_on_something_not_in_the_queue_is_refused(world: dict[str, Any]) -> None:
    client = _signed_in("reviewer")
    response = client.post(
        "/labels", json={"revid": 999_999, "verdict": "bad_edit", "confidence": "high"}
    )
    assert response.status_code == 404


# --- Automation freeze (SRS FR-37, M6-FR-25) ---


@pytest.mark.db
def test_freeze_state_defaults_to_unfrozen(world: dict[str, Any]) -> None:
    body = _signed_in("viewer").get("/admin/freeze").json()
    assert body["frozen"] is False


@pytest.mark.db
def test_an_admin_can_freeze_automation(world: dict[str, Any]) -> None:
    client = _signed_in("admin")
    response = client.post("/admin/freeze", json={"frozen": True, "reason": "testing"})
    assert response.status_code == 200
    assert response.json()["frozen"] is True

    state = client.get("/admin/freeze").json()
    assert state["frozen"] is True
    assert state["reason"] == "testing"


@pytest.mark.db
def test_an_admin_can_unfreeze_automation(world: dict[str, Any]) -> None:
    client = _signed_in("admin")
    client.post("/admin/freeze", json={"frozen": True})
    client.post("/admin/freeze", json={"frozen": False, "reason": "all clear"})

    state = client.get("/admin/freeze").json()
    assert state["frozen"] is False


@pytest.mark.db
def test_freeze_is_audited(world: dict[str, Any]) -> None:
    _signed_in("admin").post("/admin/freeze", json={"frozen": True, "reason": "drill"})

    with connect() as conn:
        row = conn.execute(
            "SELECT action, outcome, detail FROM app.audit_log ORDER BY audit_id DESC LIMIT 1"
        ).fetchone()
    assert row["action"] == "set_freeze"
    assert row["outcome"] == "allowed"
    assert "frozen=True" in (row["detail"] or "")


@pytest.mark.db
def test_a_non_admin_cannot_freeze(world: dict[str, Any]) -> None:
    """The database enforces this too (FORCE RLS), but the API should refuse first."""
    response = _signed_in("reviewer").post("/admin/freeze", json={"frozen": True})
    assert response.status_code == 403


@pytest.mark.db
def test_freeze_requires_authentication(world: dict[str, Any]) -> None:
    assert TestClient(app).get("/admin/freeze").status_code == 401


@pytest.mark.db
def test_freeze_post_requires_csrf(world: dict[str, Any]) -> None:
    client = _signed_in("admin")
    del client.headers[sessions.CSRF_HEADER]
    response = client.post("/admin/freeze", json={"frozen": True})
    assert response.status_code == 403


@pytest.mark.db
def test_the_queue_shows_a_reviewer_their_own_judgement_and_not_a_colleagues(
    world: dict[str, Any],
) -> None:
    """The policy on app.human_labels permits reading all of them; the queue
    joins on the acting user so a reviewer is not shown a colleague's opinion
    beside an edit they are about to judge."""
    _signed_in("reviewer").post(
        "/labels", json={"revid": 1, "verdict": "bad_edit", "confidence": "high"}
    )

    mine = _signed_in("reviewer").get("/queue").json()
    theirs = _signed_in("admin").get("/queue").json()

    assert next(i for i in mine["items"] if i["revid"] == 1)["my_verdict"] == "bad_edit"
    assert next(i for i in theirs["items"] if i["revid"] == 1)["my_verdict"] is None


@pytest.mark.db
def test_only_predictions_from_the_serving_champion_appear(world: dict[str, Any]) -> None:
    """M6-FR-13. A challenger in shadow writes a second score for every event,
    and two numbers beside one edit with nothing to say which is served is worse
    than showing neither."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO register.model_registry "
            "(model_version, training_start, training_end, n_train_events, n_train_positives, "
            " feature_names, hyperparameters, offline_metrics, artifact_path, artifact_sha256) "
            "VALUES ('chal', now() - interval '20 days', now() - interval '19 days', 100, 10, "
            "        ARRAY['a'], '{}'::jsonb, '{}'::jsonb, 'models/y.pkl', %s)",
            ("b" * 64,),
        )
        conn.execute(
            "INSERT INTO register.predictions "
            "(revid, event_ts, scored_at, model_version, role, score, feature_hash, "
            " outcome_observable_at_scoring) "
            "SELECT revid, event_ts, event_ts + interval '5 minutes', 'chal', 'shadow', "
            "       0.99, 'h', false FROM landing.rc_events WHERE revid = 1"
        )

    body = _signed_in("viewer").get("/queue").json()
    # One row per revision, not two. The model_version is withheld until the
    # reviewer has judged, so the shadow score's absence is checked by counting
    # rows rather than by reading a field the page no longer returns.
    assert [i["revid"] for i in body["items"]].count(1) == 1


@pytest.mark.db
def test_the_window_can_reach_past_the_maturity_horizon(world: dict[str, Any]) -> None:
    """The cap has to EXCEED the maturity window.

    Capped at 168 hours — which is the maturity window — no matured item could
    ever appear, `matured` would be a constant false, and the marker FR-41
    requires would distinguish nothing. A reviewer would also never see how any
    judgement of theirs turned out.
    """
    from api import review

    assert review.MAX_WINDOW_HOURS > 168

    body = _signed_in("viewer").get("/queue", params={"hours": 10_000}).json()
    assert body["window_hours"] == review.MAX_WINDOW_HOURS
    assert body["matured"] >= 1, "a queue that can never show a matured item is not one"


@pytest.mark.db
def test_the_queue_works_before_anything_has_been_promoted(world: dict[str, Any]) -> None:
    """The state production was actually in, and the queue could not serve it.

    decide.champion_history is empty until the first promotion. registry.champion()
    falls back to the newest registered model; the queue re-implemented that
    resolution in SQL and left the fallback out, so its `serving` CTE matched
    nothing and it returned zero items over a register holding forty-five
    thousand predictions — no error, just an empty list.
    """
    with connect() as conn:
        conn.execute("DELETE FROM decide.champion_history")

    body = _signed_in("viewer").get("/queue", params={"hours": 336}).json()
    assert len(body["items"]) > 0, "an unpromoted champion still serves the queue"


@pytest.mark.db
def test_a_promotion_overrides_the_fallback(world: dict[str, Any]) -> None:
    """And the fallback must not win once a decision exists — otherwise a
    rollback would be ignored by the one page a reviewer actually reads."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO register.model_registry "
            "(model_version, trained_at, training_start, training_end, n_train_events, "
            " n_train_positives, feature_names, hyperparameters, offline_metrics, "
            " artifact_path, artifact_sha256) "
            "VALUES ('newer', now(), now() - interval '20 days', now() - interval '19 days', "
            "        100, 10, ARRAY['a'], '{}'::jsonb, '{}'::jsonb, 'models/y.pkl', %s)",
            ("b" * 64,),
        )
        # 'champ' is older but is what the log promoted. The queue must follow
        # the decision, not the training date.
        conn.execute("DELETE FROM decide.champion_history")
        conn.execute("INSERT INTO decide.champion_history (model_version) VALUES ('champ')")

    client = _signed_in("viewer")
    body = client.get("/queue", params={"hours": 336}).json()
    # 'newer' is registered but not promoted, so nothing it scored may appear.
    # Checked by revid, because the version is withheld before judgement.
    assert {i["revid"] for i in body["items"]} <= {1, 2}
