"""Passwords and sessions (M6 §3).

The first code in this project whose failure is a break-in rather than a wrong
number. None of the habits that made the pipeline careful — leak guards,
maturity windows, pre-registered thresholds — apply to any of it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bellwether import auth

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


def test_a_password_verifies_against_its_own_hash() -> None:
    digest, salt, params = auth.hash_password("correct horse battery staple")
    assert auth.verify_password(
        "correct horse battery staple", digest=digest, salt=salt, params=params
    )


def test_a_wrong_password_does_not() -> None:
    digest, salt, params = auth.hash_password("correct horse battery staple")
    assert not auth.verify_password(
        "Correct horse battery staple", digest=digest, salt=salt, params=params
    )


def test_the_same_password_hashes_differently_for_two_users() -> None:
    """Per-user salt. Without it, identical passwords produce identical hashes
    and a leaked table tells an attacker which accounts to try first."""
    first, _, _ = auth.hash_password("same password")
    second, _, _ = auth.hash_password("same password")
    assert first != second


def test_verification_uses_the_parameters_the_row_was_made_with() -> None:
    """So the cost can be raised later without invalidating every existing
    password. A verifier that read the module constant would reject every user
    the moment the constant changed."""
    weak = {"n": 2**10, "r": 8, "p": 1, "dklen": 64}
    digest, salt, _ = auth.hash_password("legacy", salt=b"0" * 16)
    import hashlib

    old = hashlib.scrypt(b"legacy", salt=b"0" * 16, maxmem=auth.SCRYPT_MAXMEM, **weak)
    assert auth.verify_password("legacy", digest=old, salt=b"0" * 16, params=weak)
    assert not auth.verify_password("legacy", digest=old, salt=b"0" * 16, params=auth.KDF_PARAMS)
    assert digest != old


def test_a_session_token_is_returned_once_and_stored_hashed() -> None:
    """A session table holding plaintext tokens is a credential store: one
    database read is a login as anybody."""
    token, stored = auth.new_session_token()
    assert isinstance(token, str) and len(token) > 32
    assert stored == auth.hash_token(token)
    assert token.encode() not in stored


def test_two_session_tokens_are_never_the_same() -> None:
    assert len({auth.new_session_token()[0] for _ in range(500)}) == 500


def test_a_revoked_session_is_dead_however_fresh() -> None:
    assert not auth.session_is_live(
        expires_at=NOW + timedelta(hours=6),
        last_seen_at=NOW,
        revoked_at=NOW - timedelta(seconds=1),
        now=NOW,
    )


def test_an_active_session_still_expires_absolutely() -> None:
    """Idle expiry alone would let a session used every minute live forever,
    which is exactly what a stolen token does."""
    assert not auth.session_is_live(
        expires_at=NOW - timedelta(seconds=1),
        last_seen_at=NOW,
        revoked_at=None,
        now=NOW,
    )


def test_an_abandoned_session_expires_before_its_absolute_deadline() -> None:
    """Absolute expiry alone would leave a walked-away-from browser signed in
    for the rest of the day."""
    assert not auth.session_is_live(
        expires_at=NOW + timedelta(hours=6),
        last_seen_at=NOW - timedelta(minutes=auth.SESSION_IDLE_MINUTES + 1),
        revoked_at=None,
        now=NOW,
    )


def test_a_live_session_is_live() -> None:
    assert auth.session_is_live(
        expires_at=NOW + timedelta(hours=6),
        last_seen_at=NOW - timedelta(minutes=5),
        revoked_at=None,
        now=NOW,
    )


def test_generated_passwords_are_unique_and_transcribable() -> None:
    """A human types this once from a terminal into a login form. A
    24-character random string gets mistyped and then written on paper."""
    passwords = {auth.generate_password() for _ in range(200)}
    assert len(passwords) == 200
    for character in "l01O":
        assert character not in "".join(passwords), (
            "ambiguous characters invite transcription errors"
        )


def test_csrf_tokens_come_from_secrets_not_random() -> None:
    """`random` is a Mersenne Twister whose entire future output is recoverable
    from 624 observations."""
    import inspect

    source = inspect.getsource(auth)
    assert "import secrets" in source
    assert "import random" not in source
