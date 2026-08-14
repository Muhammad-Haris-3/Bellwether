"""Row-level security (SRS FR-39, M6-FR-9 to FR-11, acceptance D-3).

D-3 is the criterion the milestone turns on: **with the application's own role
checks removed, the database must still refuse.** Every other M6 criterion can
be satisfied by careful Python. Only this one distinguishes "we check the role"
from "the database enforces the role", and the only way to show it is to skip
the application layer entirely and watch Postgres say no.

So none of these tests go through the API. They connect as `bellwether_app`,
set the acting user the way a request would, and try things directly.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Json

from bellwether import auth
from bellwether.db import connect

APP_PASSWORD = "rls-test-password"  # noqa: S105 - local test role, never deployed


def _owner_url() -> str:
    url = os.environ.get("BELLWETHER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("BELLWETHER_TEST_DATABASE_URL is not set")
    return url


@pytest.fixture
def people(fresh_db: None) -> dict[str, uuid.UUID]:
    """One user of each role, plus a login for bellwether_app.

    The role is NOLOGIN in the migration — nothing in production connects as it
    unless a password is set deliberately — so the test gives it one on the
    local database only.
    """
    ids: dict[str, uuid.UUID] = {}
    with connect() as conn:
        for role in ("viewer", "reviewer", "admin"):
            digest, salt, params = auth.hash_password("x", salt=b"0" * 16)
            row = conn.execute(
                "INSERT INTO app.users (email, password_hash, password_salt, kdf_params, role) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING user_id",
                (f"{role}@example.test", digest, salt, Json(params), role),
            ).fetchone()
            ids[role] = row["user_id"]

        conn.execute("ALTER ROLE bellwether_app WITH LOGIN PASSWORD " + f"'{APP_PASSWORD}'")
    return ids


def _as_app(acting: uuid.UUID | None) -> psycopg.Connection:
    """A connection as the application role, with the acting user set exactly
    the way a request sets it."""
    tail = _owner_url().split("@", 1)[1]
    conn = psycopg.connect(
        f"postgresql://bellwether_app:{APP_PASSWORD}@{tail}", row_factory=dict_row
    )
    if acting is not None:
        conn.execute("SELECT set_config('bellwether.user_id', %s, false)", (str(acting),))
    return conn


def _event(conn: Any, revid: int) -> None:
    conn.execute(
        "INSERT INTO landing.rc_events "
        "(revid, event_ts, ns, title, is_anon, is_temp, is_minor, is_bot, "
        " sampling_stratum, sampling_weight, ingested_at_utc) "
        "VALUES (%s, now(), 0, 'Page', false, false, false, false, "
        "        'registered', 33.3, now())",
        (revid,),
    )


@pytest.mark.db
def test_the_app_role_cannot_write_to_any_evidence(people: dict[str, Any]) -> None:
    """M6-FR-8 and NFR-8. M6 is the first milestone that gives the serving
    container any write capability at all, so this is where that promise is
    kept or quietly lost."""
    forbidden = [
        "INSERT INTO register.predictions (revid, event_ts, scored_at, model_version, role, "
        "score, feature_hash) VALUES (-1, now(), now(), 'x', 'champion', 0.5, 'h')",
        "INSERT INTO outcome.labels (revid, label, label_source, first_observed_at_utc, "
        "detection_latency_seconds) VALUES (-1, true, 'mw_reverted', now(), 1)",
        "INSERT INTO decide.model_decisions (decision, champion_version) VALUES ('promote', 'x')",
        "UPDATE register.predictions SET score = 0 WHERE revid = -1",
        "DELETE FROM landing.rc_events WHERE revid = -1",
    ]
    with _as_app(people["admin"]) as conn:
        for statement in forbidden:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()


@pytest.mark.db
def test_a_viewer_cannot_write_a_human_label(people: dict[str, Any]) -> None:
    """D-3. No application code is involved in this refusal — the request never
    reaches Python, because the policy decides first."""
    with connect() as conn:
        _event(conn, 1)

    with _as_app(people["viewer"]) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(
            "INSERT INTO app.human_labels (revid, user_id, verdict, confidence) "
            "VALUES (1, %s, 'bad_edit', 'high')",
            (people["viewer"],),
        )


@pytest.mark.db
def test_a_reviewer_cannot_record_a_judgement_as_somebody_else(people: dict[str, Any]) -> None:
    """The WITH CHECK on user_id. Without it a reviewer could attribute an
    opinion to a colleague, and M7's agreement study would be measuring a
    fabrication."""
    with connect() as conn:
        _event(conn, 2)

    with _as_app(people["reviewer"]) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(
            "INSERT INTO app.human_labels (revid, user_id, verdict, confidence) "
            "VALUES (2, %s, 'bad_edit', 'high')",
            (people["admin"],),
        )


@pytest.mark.db
def test_a_reviewer_can_record_their_own_judgement(people: dict[str, Any]) -> None:
    """The control. Without it every test above could be passing because
    nothing can write anything at all."""
    with connect() as conn:
        _event(conn, 3)

    with _as_app(people["reviewer"]) as conn:
        conn.execute(
            "INSERT INTO app.human_labels (revid, user_id, verdict, confidence) "
            "VALUES (3, %s, 'good_edit', 'medium')",
            (people["reviewer"],),
        )
        conn.commit()

    with connect() as conn:
        row = conn.execute("SELECT count(*) AS n FROM app.human_labels").fetchone()
    assert row["n"] == 1


@pytest.mark.db
def test_an_unauthenticated_connection_has_no_rights_at_all(people: dict[str, Any]) -> None:
    """The policies fail closed on a NULL acting user. An unauthenticated
    request is not a request with an empty role — it is one with no rights."""
    with _as_app(None) as conn:
        row = conn.execute("SELECT count(*) AS n FROM app.users").fetchone()
    assert row["n"] == 0


@pytest.mark.db
def test_a_user_cannot_read_another_users_row(people: dict[str, Any]) -> None:
    with _as_app(people["viewer"]) as conn:
        rows = conn.execute("SELECT user_id FROM app.users").fetchall()
    assert [r["user_id"] for r in rows] == [people["viewer"]]


@pytest.mark.db
def test_an_admin_sees_every_user_but_still_not_their_sessions(people: dict[str, Any]) -> None:
    """An administrator who can read session rows can impersonate every user in
    the system, and no feature here needs that."""
    with connect() as conn:
        _token, digest = auth.new_session_token()
        conn.execute(
            "INSERT INTO app.sessions (user_id, token_hash, expires_at) "
            "VALUES (%s, %s, now() + interval '1 hour')",
            (people["reviewer"], digest),
        )

    with _as_app(people["admin"]) as conn:
        users = conn.execute("SELECT count(*) AS n FROM app.users").fetchone()
        sessions = conn.execute("SELECT count(*) AS n FROM app.sessions").fetchone()

    assert users["n"] == 3
    assert sessions["n"] == 0, "not even an admin reads somebody else's session"


@pytest.mark.db
def test_only_an_admin_reads_the_audit_log(people: dict[str, Any]) -> None:
    """A reviewer able to read it can see every colleague's activity, which is
    surveillance rather than accountability. Appending is open to all."""
    with _as_app(people["reviewer"]) as conn:
        conn.execute(
            "INSERT INTO app.audit_log (actor, action, outcome) VALUES (%s, 'sign_in', 'allowed')",
            (people["reviewer"],),
        )
        conn.commit()
        mine = conn.execute("SELECT count(*) AS n FROM app.audit_log").fetchone()
    assert mine["n"] == 0

    with _as_app(people["admin"]) as conn:
        theirs = conn.execute("SELECT count(*) AS n FROM app.audit_log").fetchone()
    assert theirs["n"] == 1


@pytest.mark.db
def test_only_an_admin_can_freeze_automation(people: dict[str, Any]) -> None:
    """SRS FR-37. An admin may halt promotion; nobody may rewrite a decision."""
    with _as_app(people["reviewer"]) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(
            "INSERT INTO app.automation_freeze (frozen, actor) VALUES (true, %s)",
            (people["reviewer"],),
        )

    with _as_app(people["admin"]) as conn:
        conn.execute(
            "INSERT INTO app.automation_freeze (frozen, actor, reason) "
            "VALUES (true, %s, 'investigating a drift alert')",
            (people["admin"],),
        )
        conn.commit()

    with connect() as conn:
        row = conn.execute("SELECT frozen FROM app.automation_freeze").fetchone()
    assert row["frozen"] is True


@pytest.mark.db
def test_the_pre_auth_door_cannot_be_turned_into_a_user_list(people: dict[str, Any]) -> None:
    """sql/023 exists because the policy protecting app.users forbids the query
    that would satisfy it — sign-in must read the table before there is an
    acting user to gate on.

    The door is narrow on purpose: it takes an exact address and returns at most
    one row. It has no predicate to widen, so holding the grant lets a caller
    check one address at a time and never enumerate.
    """
    with _as_app(None) as conn:
        found = conn.execute(
            "SELECT * FROM app.credentials_for(%s)", ("reviewer@example.test",)
        ).fetchall()
        absent = conn.execute(
            "SELECT * FROM app.credentials_for(%s)", ("nobody@example.test",)
        ).fetchall()
        # The table itself stays shut, even to the role that may call the door.
        direct = conn.execute("SELECT count(*) AS n FROM app.users").fetchone()

    assert len(found) == 1
    assert absent == []
    assert direct["n"] == 0


@pytest.mark.db
def test_the_session_door_resolves_a_token_and_nothing_else(people: dict[str, Any]) -> None:
    """Keyed on the token hash rather than on a user, so the grant lets a caller
    resolve a token it already holds and cannot ask whose sessions exist."""
    token, digest = auth.new_session_token()
    with connect() as conn:
        conn.execute(
            "INSERT INTO app.sessions (user_id, token_hash, expires_at) "
            "VALUES (%s, %s, now() + interval '1 hour')",
            (people["reviewer"], digest),
        )

    with _as_app(None) as conn:
        mine = conn.execute("SELECT * FROM app.session_for(%s)", (digest,)).fetchall()
        other = conn.execute(
            "SELECT * FROM app.session_for(%s)", (auth.hash_token("guessed"),)
        ).fetchall()

    assert len(mine) == 1 and mine[0]["role"] == "reviewer"
    assert other == []
