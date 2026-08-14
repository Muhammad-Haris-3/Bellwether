"""Sign-in, sign-out, and what the endpoint refuses to tell you (M6 §3, §9).

The tests that matter here are the negative ones. A sign-in endpoint that works
is easy; one that does not quietly answer "is this person registered?" takes
deliberate effort, and nothing about it shows up in a happy path.
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

APP_PASSWORD = "session-test-password"  # noqa: S105 - local test role, never deployed
USER_PASSWORD = "known-good-password"  # noqa: S105


@pytest.fixture
def account(fresh_db: None, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A reviewer, and an application credential pointed at the test database."""
    url = os.environ.get("BELLWETHER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("BELLWETHER_TEST_DATABASE_URL is not set")

    digest, salt, params = auth.hash_password(USER_PASSWORD)
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO app.users (email, password_hash, password_salt, kdf_params, role) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING user_id",
            ("reviewer@example.test", digest, salt, Json(params), "reviewer"),
        ).fetchone()
        conn.execute("ALTER ROLE bellwether_app WITH LOGIN PASSWORD " + f"'{APP_PASSWORD}'")

    tail = url.split("@", 1)[1]
    monkeypatch.setenv(
        "BELLWETHER_APP_DATABASE_URL", f"postgresql://bellwether_app:{APP_PASSWORD}@{tail}"
    )
    get_settings.cache_clear()

    # The limiter is process-local, so one test's failed attempts would
    # otherwise count against the next one's.
    sessions._attempts.clear()

    return {"user_id": row["user_id"], "email": "reviewer@example.test"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.mark.db
def test_a_correct_password_starts_a_session(client: TestClient, account: dict[str, Any]) -> None:
    response = client.post(
        "/auth/sign-in", json={"email": account["email"], "password": USER_PASSWORD}
    )
    assert response.status_code == 200
    assert response.json()["role"] == "reviewer"

    assert sessions.SESSION_COOKIE in response.cookies
    assert sessions.CSRF_COOKIE in response.cookies

    with connect() as conn:
        row = conn.execute("SELECT token_hash FROM app.sessions").fetchone()
    # Stored hashed. One database read must not be a login as anybody.
    assert bytes(row["token_hash"]) == auth.hash_token(response.cookies[sessions.SESSION_COOKIE])


@pytest.mark.db
def test_an_unknown_address_and_a_wrong_password_are_indistinguishable(
    client: TestClient, account: dict[str, Any]
) -> None:
    """M6-FR-4 and FR-33. Different answers turn the sign-in form into a
    directory of who has an account here."""
    unknown = client.post(
        "/auth/sign-in", json={"email": "nobody@example.test", "password": "whatever"}
    )
    wrong = client.post(
        "/auth/sign-in", json={"email": account["email"], "password": "not the password"}
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


@pytest.mark.db
def test_a_failed_sign_in_is_recorded(client: TestClient, account: dict[str, Any]) -> None:
    """A log of successful actions describes what the system did. One including
    refusals describes what it was asked to do."""
    client.post("/auth/sign-in", json={"email": account["email"], "password": "wrong"})

    with connect() as conn:
        row = conn.execute(
            "SELECT action, outcome, detail FROM app.audit_log ORDER BY audit_id DESC LIMIT 1"
        ).fetchone()
    assert row["action"] == "sign_in"
    assert row["outcome"] == "refused"
    # Never the password, never the token.
    assert "wrong" not in (row["detail"] or "")


@pytest.mark.db
def test_repeated_attempts_are_rate_limited(client: TestClient, account: dict[str, Any]) -> None:
    """M6-FR-32. Reading is scraped; a login is guessed, and the two need
    different limits."""
    for _ in range(sessions.SIGNIN_MAX_ATTEMPTS):
        client.post("/auth/sign-in", json={"email": account["email"], "password": "wrong"})

    blocked = client.post(
        "/auth/sign-in", json={"email": account["email"], "password": USER_PASSWORD}
    )
    assert blocked.status_code == 429, "a correct password must not bypass the limiter"


@pytest.mark.db
def test_signing_out_revokes_the_row_not_just_the_cookie(
    client: TestClient, account: dict[str, Any]
) -> None:
    """M6-FR-5. Clearing a cookie signs out a browser; revoking the row signs
    out whoever else is holding that token, which is the case that matters."""
    client.post("/auth/sign-in", json={"email": account["email"], "password": USER_PASSWORD})
    token = client.cookies[sessions.SESSION_COOKIE]

    client.post("/auth/sign-out")

    with connect() as conn:
        row = conn.execute("SELECT revoked_at FROM app.sessions").fetchone()
    assert row["revoked_at"] is not None

    # And the token is dead even for someone who still holds it.
    assert sessions.current_user(token) is None


@pytest.mark.db
def test_a_revoked_token_is_nobody(client: TestClient, account: dict[str, Any]) -> None:
    client.post("/auth/sign-in", json={"email": account["email"], "password": USER_PASSWORD})
    token = client.cookies[sessions.SESSION_COOKIE]
    assert sessions.current_user(token) is not None

    with connect() as conn:
        conn.execute("UPDATE app.sessions SET revoked_at = now()")

    assert sessions.current_user(token) is None


@pytest.mark.db
def test_a_deactivated_account_cannot_use_a_live_session(
    client: TestClient, account: dict[str, Any]
) -> None:
    """Revoking access has to take effect on the session someone already holds,
    not only on their next sign-in."""
    client.post("/auth/sign-in", json={"email": account["email"], "password": USER_PASSWORD})
    token = client.cookies[sessions.SESSION_COOKIE]

    with connect() as conn:
        conn.execute("UPDATE app.users SET is_active = false")

    assert sessions.current_user(token) is None


@pytest.mark.db
def test_an_unknown_token_is_nobody(account: dict[str, Any]) -> None:
    assert sessions.current_user("not-a-real-token") is None


@pytest.mark.db
def test_me_reports_anonymity_without_erroring(client: TestClient, account: dict[str, Any]) -> None:
    """ "Nobody is signed in" is the ordinary state of a public page, not a
    failure the frontend should render as one."""
    body = client.get("/auth/me").json()
    assert body["authenticated"] is False
    assert body["auth_configured"] is True


@pytest.mark.db
def test_no_response_ever_contains_a_hash_or_a_token_field(
    client: TestClient, account: dict[str, Any]
) -> None:
    """M6-FR-6. The session token reaches the browser in a cookie and nowhere
    else; a hash reaches it never."""
    signed_in = client.post(
        "/auth/sign-in", json={"email": account["email"], "password": USER_PASSWORD}
    )
    body = signed_in.text
    for forbidden in ("password_hash", "password_salt", "token_hash", USER_PASSWORD):
        assert forbidden not in body

    me = client.get("/auth/me").text
    for forbidden in ("password_hash", "token_hash", "user_id"):
        assert forbidden not in me


def test_the_session_cookie_is_httponly_and_the_csrf_cookie_is_not() -> None:
    """Double-submit needs the page to READ the CSRF value and echo it in a
    header, which is exactly what the session cookie must never allow."""
    import inspect

    source = inspect.getsource(sessions.sign_in)
    session_block = source[source.index("SESSION_COOKIE,") : source.index("CSRF_COOKIE,")]
    csrf_block = source[source.index("CSRF_COOKIE,") :]
    assert "httponly=True" in session_block
    assert "httponly=False" in csrf_block


@pytest.mark.db
def test_csrf_requires_the_header_to_match_the_cookie(
    client: TestClient, account: dict[str, Any]
) -> None:
    from fastapi import HTTPException, Request

    def request_with(cookies: dict[str, str]) -> Request:
        scope = {
            "type": "http",
            "headers": [(b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode())],
        }
        return Request(scope)

    token = auth.new_csrf_token()
    sessions.check_csrf(request_with({sessions.CSRF_COOKIE: token}), header=token)

    for cookies, header in (
        ({sessions.CSRF_COOKIE: token}, "something else"),
        ({sessions.CSRF_COOKIE: token}, None),
        ({}, token),
    ):
        with pytest.raises(HTTPException):
            sessions.check_csrf(request_with(cookies), header=header)


@pytest.mark.db
def test_authentication_is_unavailable_rather_than_privileged_when_unconfigured(
    client: TestClient, fresh_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NFR-8. An application credential that silently fell back to the writer
    would hand the serving container write access to every evidential table, so
    an unconfigured deployment loses features rather than gaining privileges."""
    monkeypatch.setenv("BELLWETHER_APP_DATABASE_URL", "")
    get_settings.cache_clear()

    response = client.post("/auth/sign-in", json={"email": "a@b.test", "password": "x"})
    assert response.status_code == 503


def test_the_acting_user_is_never_interpolated_into_sql() -> None:
    """The policies cast `bellwether.user_id` to uuid and every one of them
    trusts it. Building that setting by string formatting would be an injection
    into the single value the whole access-control model rests on, so it is
    always a bound parameter and always read out of the sessions table."""
    import inspect

    source = inspect.getsource(sessions)
    assert "set_config('bellwether.user_id', %s" in source
    assert "set_config('bellwether.user_id', '" not in source
    assert 'f"SELECT set_config' not in source
