"""API behaviour, including what it must never say.

The endpoints are public and unauthenticated, so a leak here is a publication.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_health_answers_even_with_no_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Degraded, not dead.

    A health endpoint that fails when the database is unreachable removes the
    one page that would have said why.
    """
    from bellwether.config import get_settings

    monkeypatch.setenv(
        "BELLWETHER_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none?connect_timeout=1"
    )
    monkeypatch.setenv(
        "BELLWETHER_READONLY_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none?connect_timeout=1"
    )
    get_settings.cache_clear()

    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["database_reachable"] is False


def test_health_never_returns_a_password(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident this guards against happened on a previous project.

    A connection string reached a public health endpoint and served the
    password until someone noticed.
    """
    from bellwether.config import get_settings

    secret = "sup3rs3cr3tpassw0rd"  # noqa: S105
    monkeypatch.setenv(
        "BELLWETHER_READONLY_DATABASE_URL",
        f"postgresql://reader:{secret}@db.example.com:5432/bellwether",
    )
    monkeypatch.setenv("BELLWETHER_ENV", f"postgresql://owner:{secret}@db.example.com/x")
    get_settings.cache_clear()

    raw = client.get("/health").text
    assert secret not in raw
    assert "misconfigured" in raw


def test_health_reports_a_bad_env_label_without_echoing_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bellwether.config import get_settings

    monkeypatch.setenv("BELLWETHER_ENV", "not a valid label at all")
    get_settings.cache_clear()

    body = client.get("/health").json()
    assert body["env"] == "misconfigured"
    assert body["env_is_valid"] is False


def test_error_detail_is_a_class_name_not_a_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """psycopg's connection errors quote the host, the user and sometimes the
    whole connection string."""
    from bellwether.config import get_settings

    monkeypatch.setenv(
        "BELLWETHER_READONLY_DATABASE_URL",
        "postgresql://reader:leakme@127.0.0.1:1/none?connect_timeout=1",
    )
    get_settings.cache_clear()

    body = client.get("/health").json()
    assert body["error_class"] is not None
    assert " " not in body["error_class"]
    assert "leakme" not in str(body)


def test_serving_requirements_carry_no_http_client() -> None:
    """The API must not ship the code that talks to MediaWiki.

    "The API cannot write" is a stronger claim than "the API does not write",
    and it stops being true the moment the serving image contains the client.
    """
    # Requirement lines only. The file's own comment explains which packages
    # are excluded and why, so scanning the raw text made the test fail on the
    # documentation of the rule it was enforcing.
    lines = [
        line.strip().lower()
        for line in (REPO / "requirements-serve.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    for forbidden in ("httpx", "tenacity", "requests"):
        assert not any(line.startswith(forbidden) for line in lines), (
            f"{forbidden} must not be in the serving image"
        )


def test_the_dockerfile_installs_only_serving_requirements() -> None:
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "requirements-serve.txt" in dockerfile
    assert "requirements-pipeline.txt" not in dockerfile
    assert "requirements-dev.txt" not in dockerfile


def test_render_blueprint_does_not_contain_a_connection_string() -> None:
    """sync: false means "ask me in the dashboard". A URL committed here would
    be a credential in a public repository."""
    blueprint = (REPO / "render.yaml").read_text(encoding="utf-8")
    assert "postgresql://" not in blueprint
    assert "sync: false" in blueprint


@pytest.mark.db
def test_stats_reports_rates_only_over_matured_edits(client: TestClient, fresh_db: None) -> None:
    body = client.get("/stats").json()
    assert "mature_48h" in body
    assert "runs" in body
    for row in body["mature_48h"]:
        assert row["n"] > 0, "a rate must never be published without its sample size"
