from __future__ import annotations

import pytest

from bellwether.config import Settings


def test_env_label_accepts_a_short_label() -> None:
    assert Settings(env="production").env == "production"


def test_env_label_rejects_a_connection_string() -> None:
    """The incident this guards against actually happened on a prior project.

    A connection string was pasted into the environment-label variable and the
    public health endpoint served the password until it was noticed. The
    validator must neutralise it rather than pass it through.
    """
    leaked = "postgresql://user:hunter2@ep-something.aws.neon.tech/db"
    settings = Settings(env=leaked)
    assert settings.env == "misconfigured"
    assert not settings.env_is_valid
    assert "hunter2" not in settings.env


def test_user_agent_carries_contact_details() -> None:
    """Wikimedia's 200 req/min tier requires a UA with a URL and an address.

    Without one the limit is 10 req/min, which will not run this project — so
    a malformed UA is a capacity failure, not a cosmetic one.
    """
    ua = Settings().user_agent
    assert ua.startswith("Bellwether/")
    assert "github.com" in ua
    assert "@" in ua


def test_roles_must_be_distinct() -> None:
    same = "postgresql://x:y@host/db"
    with pytest.raises(ValueError, match="read-only role"):
        Settings(database_url=same, readonly_database_url=same).assert_roles_distinct()


def test_distinct_roles_pass() -> None:
    Settings(
        database_url="postgresql://writer:y@host/db",
        readonly_database_url="postgresql://reader:z@host/db",
    ).assert_roles_distinct()


def test_serving_host_strips_credentials() -> None:
    settings = Settings(
        readonly_database_url="postgresql://u:secret@example.com/db?sslmode=require"
    )
    assert "secret" not in settings.serving_host
    assert settings.serving_host == "example.com/db"
