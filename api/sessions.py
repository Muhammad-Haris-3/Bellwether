"""Sign-in, sign-out, and who is asking (M6 §3, §9).

Separated from `api/main.py` because everything here fails differently from
everything there. A wrong query in main.py publishes a wrong number; a wrong
check here lets someone in.

**Every authenticated query runs with the acting user set.** `SET LOCAL` inside
the transaction, so the row-level policies decide — the Python role checks in
this file are a courtesy that produces a clean 403 instead of an empty result,
and D-3 requires that removing them changes nothing about what is permitted.

**Failures are recorded.** A log of successful actions describes what the system
did; one including refusals describes what it was asked to do, and only the
second shows an access control working.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import psycopg
from fastapi import Cookie, Header, HTTPException, Request, Response
from psycopg.rows import dict_row
from pydantic import BaseModel, field_validator

from bellwether import auth
from bellwether.config import get_settings

SESSION_COOKIE = "bellwether_session"
CSRF_COOKIE = "bellwether_csrf"
CSRF_HEADER = "x-csrf-token"

# Sign-in is rate limited separately from reading (M6-FR-32), because the two
# are attacked differently: a queue is scraped, a login is guessed. Process
# local, which is all a single container has — and is honest about it rather
# than implying a distributed limiter that does not exist.
SIGNIN_MAX_ATTEMPTS = 8
SIGNIN_WINDOW_SECONDS = 300
_attempts: dict[str, list[float]] = defaultdict(list)


class Credentials(BaseModel):
    """Deliberately not `EmailStr`.

    Pydantic's version needs `email-validator`, which would be a new dependency
    in the serving image for the sake of rejecting addresses this application
    never sends mail to — the address is an identifier here, not a channel. CI
    asserts the serving requirements stay minimal, and a syntactic check is all
    that is warranted.
    """

    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _looks_like_an_address(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value or len(value) < 3 or len(value) > 254:
            raise ValueError("not an address")
        return value


def _app_connection() -> psycopg.Connection:
    """A connection as `bellwether_app`, never as the writer.

    There is deliberately no fallback. An application credential that silently
    became the pipeline's would hand the serving container write access to
    every evidential table, which is the one thing NFR-8 forbids — so an
    unconfigured deployment loses authenticated features rather than gaining
    privileges.
    """
    settings = get_settings()
    if not settings.app_role_configured:
        raise HTTPException(
            status_code=503,
            detail="Authentication is not configured on this deployment.",
        )
    try:
        return psycopg.connect(settings.app_database_url, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        # A wrong credential here surfaced as an unhandled 500, and FastAPI's
        # exception path attaches no CORS headers — so the browser could not
        # read the response at all and reported "Failed to fetch". The server
        # was broken and the page blamed the network.
        #
        # The class name only. psycopg puts the host, the user and sometimes
        # the whole connection string into these messages, and this endpoint is
        # public.
        raise HTTPException(
            status_code=503,
            detail=(
                "The application cannot reach its database. Check "
                "BELLWETHER_APP_DATABASE_URL on the deployment."
            ),
        ) from exc


def acting_connection(user: dict[str, Any]) -> psycopg.Connection:
    """A connection with the acting user set, so the policies decide.

    Every authenticated query goes through this. The role checks elsewhere in
    this module produce a clean 403 instead of an empty result, but they are not
    what enforces anything — D-3 requires that deleting them changes what a user
    is TOLD and never what they can do.

    The setting is always a bound parameter, never formatted into the string. It
    is the one value every policy in the schema trusts, and building it by
    concatenation would be an injection straight into the access-control model.

    `is_local = true`, so it dies with the transaction. Session-level — which is
    what the first version used — survives on the backend after the transaction
    ends, and a pooled connection hands that same backend to the next request.
    The next request could be a different user, or nobody at all, and it would
    inherit an identity every policy in this schema trusts. That is not a style
    preference; it is the difference between a scoped credential and a leaked
    one.
    """
    conn = _app_connection()
    conn.execute("SELECT set_config('bellwether.user_id', %s, true)", (str(user["user_id"]),))
    return conn


def _rate_limited(key: str) -> bool:
    now = time.monotonic()
    recent = [at for at in _attempts[key] if now - at < SIGNIN_WINDOW_SECONDS]
    _attempts[key] = recent
    return len(recent) >= SIGNIN_MAX_ATTEMPTS


def _record_attempt(key: str) -> None:
    _attempts[key].append(time.monotonic())


def audit(
    conn: psycopg.Connection,
    *,
    actor: Any,
    actor_role: str | None,
    action: str,
    outcome: str,
    target: str | None = None,
    detail: str | None = None,
    request_ip: str | None = None,
) -> None:
    """Append to the audit log, including refusals (M6-FR-24).

    Never the token, never a password, never a connection string — `detail` is
    for what was attempted, not for what was supplied.
    """
    conn.execute(
        "INSERT INTO app.audit_log "
        "(actor, actor_role, action, target, outcome, detail, request_ip) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (actor, actor_role, action, target, outcome, detail, request_ip),
    )


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host[:45] if request.client else None


def sign_in(request: Request, response: Response, credentials: Credentials) -> dict[str, Any]:
    """Exchange an email and password for a session.

    The response is identical whether the address is unknown, the password is
    wrong, or the account is disabled (M6-FR-4, M6-FR-33). Distinguishing them
    turns the sign-in form into a directory of who has an account here.
    """
    email = credentials.email.strip().lower()
    ip = _client_ip(request)
    refusal = HTTPException(status_code=401, detail="Those credentials are not valid.")

    for key in (f"email:{email}", f"ip:{ip}"):
        if _rate_limited(key):
            raise HTTPException(
                status_code=429, detail="Too many sign-in attempts. Try again shortly."
            )

    with _app_connection() as conn:
        # Through the narrow SECURITY DEFINER door in sql/023, not the table.
        # The policy on app.users gates on the acting user, and there is no
        # acting user yet — the policy protecting the table forbids the query
        # that would satisfy it, so the pre-auth lookup is a function that
        # returns one address's credentials and cannot be asked for a list.
        user = conn.execute("SELECT * FROM app.credentials_for(%s)", (email,)).fetchone()

        # The KDF runs even when the address is unknown, against a dummy hash.
        # Without it the endpoint answers instantly for absent accounts and
        # slowly for present ones, which is a directory lookup with extra steps.
        if user is None:
            auth.hash_password(credentials.password, salt=b"0" * auth.SALT_BYTES)
            _record_attempt(f"email:{email}")
            _record_attempt(f"ip:{ip}")
            audit(
                conn,
                actor=None,
                actor_role=None,
                action="sign_in",
                outcome="refused",
                target=email,
                detail="no such account",
                request_ip=ip,
            )
            conn.commit()
            raise refusal

        ok = user["is_active"] and auth.verify_password(
            credentials.password,
            digest=bytes(user["password_hash"]),
            salt=bytes(user["password_salt"]),
            params=user["kdf_params"],
        )
        if not ok:
            _record_attempt(f"email:{email}")
            _record_attempt(f"ip:{ip}")
            audit(
                conn,
                actor=user["user_id"],
                actor_role=user["role"],
                action="sign_in",
                outcome="refused",
                target=email,
                detail="bad password or inactive account",
                request_ip=ip,
            )
            conn.commit()
            raise refusal

        token, token_hash = auth.new_session_token()
        conn.execute(
            "INSERT INTO app.sessions (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
            (user["user_id"], token_hash, auth.session_expiry()),
        )
        audit(
            conn,
            actor=user["user_id"],
            actor_role=user["role"],
            action="sign_in",
            outcome="allowed",
            target=email,
            request_ip=ip,
        )
        conn.commit()

    csrf = auth.new_csrf_token()
    secure = get_settings().env == "production"

    # HttpOnly on the session so script cannot read it; NOT on the CSRF cookie,
    # which the page has to read in order to echo it back in a header. That is
    # the double-submit pattern, and it is why the two are separate cookies.
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=auth.SESSION_ABSOLUTE_HOURS * 3600,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        httponly=False,
        secure=secure,
        samesite="lax",
        max_age=auth.SESSION_ABSOLUTE_HOURS * 3600,
        path="/",
    )
    return {"email": email, "role": user["role"], "csrf_token": csrf}


def current_user(
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict[str, Any] | None:
    """Who is asking, or None.

    Returns None rather than raising, so a public endpoint can call it without
    turning an anonymous visit into an error. Endpoints that require somebody
    use :func:`require_role`.
    """
    if not session:
        return None

    with _app_connection() as conn:
        # Same door, same reason: this is the lookup that ESTABLISHES the
        # acting user, so it cannot be gated on one. Keyed on the token hash, so
        # the grant lets the caller resolve a token it already holds and nothing
        # more.
        row = conn.execute(
            "SELECT * FROM app.session_for(%s)", (auth.hash_token(session),)
        ).fetchone()

        if not row or not row["is_active"]:
            return None

        now = datetime.now(UTC)
        if not auth.session_is_live(
            expires_at=row["expires_at"],
            last_seen_at=row["last_seen_at"],
            revoked_at=row["revoked_at"],
            now=now,
        ):
            return None

        # Touch, so the idle clock tracks use. The acting user must be set for
        # the policy on app.sessions to permit its own update.
        conn.execute("SELECT set_config('bellwether.user_id', %s, true)", (str(row["user_id"]),))
        conn.execute(
            "UPDATE app.sessions SET last_seen_at = now() WHERE session_id = %s",
            (row["session_id"],),
        )
        conn.commit()

    return {"user_id": row["user_id"], "email": row["email"], "role": row["role"]}


def require_role(*allowed: str):
    """A dependency that refuses, and records the refusal.

    These checks are a courtesy. The database enforces the same rules through
    row-level security, and D-3 requires that deleting this function changes
    what a user SEES — a 403 instead of an empty list — and never what they can
    DO.
    """

    def dependency(request: Request) -> dict[str, Any]:
        user = current_user(request.cookies.get(SESSION_COOKIE))
        if user is None:
            raise HTTPException(status_code=401, detail="Sign in first.")
        if user["role"] not in allowed:
            with _app_connection() as conn:
                audit(
                    conn,
                    actor=user["user_id"],
                    actor_role=user["role"],
                    action=f"{request.method} {request.url.path}",
                    outcome="refused",
                    detail=f"role {user['role']} not in {sorted(allowed)}",
                    request_ip=_client_ip(request),
                )
                conn.commit()
            raise HTTPException(status_code=403, detail="Your role does not allow that.")
        return user

    return dependency


def check_csrf(
    request: Request,
    header: str | None = Header(default=None, alias=CSRF_HEADER),
) -> None:
    """Double-submit, compared in constant time (M6-FR-30).

    SameSite=Lax already blocks the cross-site POST this defends against in
    every browser that honours it. This is the second lock, for the ones that
    do not and for the requests Lax permits.
    """
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie or not header or not auth.constant_time_equals(cookie, header):
        raise HTTPException(status_code=403, detail="Missing or invalid CSRF token.")


def sign_out(request: Request, response: Response) -> dict[str, str]:
    """Delete the session row, not merely the cookie (M6-FR-5).

    Clearing a cookie signs out a browser. Revoking the row signs out whoever
    else is holding that token, which is the case that matters.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with _app_connection() as conn:
            row = conn.execute(
                "SELECT * FROM app.session_for(%s)", (auth.hash_token(token),)
            ).fetchone()
            if row:
                conn.execute(
                    "SELECT set_config('bellwether.user_id', %s, true)", (str(row["user_id"]),)
                )
                conn.execute(
                    "UPDATE app.sessions SET revoked_at = now() WHERE session_id = %s",
                    (row["session_id"],),
                )
                audit(
                    conn,
                    actor=row["user_id"],
                    actor_role=row["role"],
                    action="sign_out",
                    outcome="allowed",
                    request_ip=_client_ip(request),
                )
            conn.commit()

    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"signed_out": "ok"}
