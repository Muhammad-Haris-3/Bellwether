"""Passwords and sessions (M6 §3, SRS FR-38 as amended 2026-08-14).

Standard library only. `hashlib.scrypt` is a memory-hard KDF and `secrets` is a
CSPRNG, so the serving image gains no dependency for the one part of this
project where a dependency would be doing security-critical work on its own
schedule (M6-FR-38).

**Nothing here stores a plaintext secret.** A password becomes a hash; a session
token is returned to the caller once and stored hashed. A session table holding
plaintext tokens is a credential store — one database read is a login as
anybody — and this project publishes its database host in a health endpoint on
purpose.

**Comparisons are constant-time.** A `==` on a token hash leaks its prefix
through timing, slowly and reliably, to anyone willing to measure.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

# scrypt parameters.
#
# n=2**15 with r=8 costs roughly 32 MB and a few tens of milliseconds per
# verification, which is affordable on a free-tier container and expensive
# enough that an offline attacker gains little from a leaked table.
#
# Stored per row rather than assumed, so these can be raised later without
# invalidating every existing password.
KDF_PARAMS: dict[str, int] = {"n": 2**15, "r": 8, "p": 1, "dklen": 64}

# OpenSSL refuses above 32 MB by default, and these parameters need exactly
# that — `hashlib.scrypt` raises "memory limit exceeded" rather than quietly
# using a cheaper cost, which is the right way round.
#
# NOT part of the stored parameters: maxmem is a ceiling on what the library
# will allocate, not an input to the derivation, so changing it cannot
# invalidate an existing hash.
SCRYPT_MAXMEM = 128 * 1024 * 1024

SALT_BYTES = 16
TOKEN_BYTES = 32  # 256 bits

# Two expiries, because one alone is wrong in a different way each time.
# Absolute bounds a stolen token; idle bounds an abandoned browser. Without the
# idle bound a session lives its full span however long ago it was used;
# without the absolute bound an active session never ends.
SESSION_ABSOLUTE_HOURS = 12
SESSION_IDLE_MINUTES = 60


def hash_password(
    password: str, *, salt: bytes | None = None
) -> tuple[bytes, bytes, dict[str, int]]:
    """Return (hash, salt, params). The password is never returned or stored."""
    salt = salt or secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, maxmem=SCRYPT_MAXMEM, **KDF_PARAMS)
    return digest, salt, dict(KDF_PARAMS)


def verify_password(password: str, *, digest: bytes, salt: bytes, params: dict[str, Any]) -> bool:
    """Constant-time verification against the parameters that row was made with.

    Reading the parameters from the row rather than the module is what allows
    them to be raised later: an old password still verifies under the cost it
    was created with, and can be re-hashed on next sign-in.
    """
    candidate = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=int(params["n"]),
        r=int(params["r"]),
        p=int(params["p"]),
        dklen=int(params["dklen"]),
        maxmem=SCRYPT_MAXMEM,
    )
    return hmac.compare_digest(candidate, digest)


def new_session_token() -> tuple[str, bytes]:
    """A session token and the hash to store.

    The plaintext is returned exactly once, to be put in a cookie. It is never
    written down. SHA-256 rather than scrypt here on purpose: the token is 256
    random bits, so there is nothing to brute-force and no reason to make every
    request pay a KDF.
    """
    token = secrets.token_urlsafe(TOKEN_BYTES)
    return token, hash_token(token)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def new_csrf_token() -> str:
    """M6-FR-30 and FR-35. `secrets`, never `random` — the latter is a Mersenne
    Twister whose entire future output is recoverable from 624 observations."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def session_expiry(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) + timedelta(hours=SESSION_ABSOLUTE_HOURS)


def session_is_live(
    *, expires_at: datetime, last_seen_at: datetime, revoked_at: datetime | None, now: datetime
) -> bool:
    """Both expiries and revocation, checked in one place.

    Checked here rather than in a WHERE clause so that the rule is one thing a
    test can hold, instead of a predicate repeated at every call site — which is
    how a session check ends up correct in three places and absent in the
    fourth.
    """
    if revoked_at is not None:
        return False
    if now >= expires_at:
        return False
    return now < last_seen_at + timedelta(minutes=SESSION_IDLE_MINUTES)


def generate_password(words: int = 4) -> str:
    """An admin-issued password, shown once (M6-FR-39).

    Length rather than symbol soup: a human has to transcribe this from a
    terminal into a login form once, and a 24-character random string gets
    mistyped and then written on paper.
    """
    alphabet = "abcdefghijkmnopqrstuvwxyz23456789"  # no l, 0, 1
    return "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(words))
