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


def test_every_migration_has_a_health_expectation() -> None:
    """The omission that made this test necessary.

    Migration 005 was written, applied to production, and reported by /health
    as... nothing at all. Not `false` — absent. `schema_behind` was empty and
    the status was `ok`, because a check that does not exist cannot fail.

    That is the same vacuous pass this project keeps finding, this time inside
    the mechanism built to detect exactly this class of drift. A missing
    expectation must break the build, not read as health.
    """
    from api.main import SCHEMA_EXPECTATIONS

    migrations = {p.stem for p in (REPO / "sql").glob("*.sql")}
    missing = migrations - SCHEMA_EXPECTATIONS.keys()
    assert not missing, f"no /health expectation for: {sorted(missing)}"

    stale = SCHEMA_EXPECTATIONS.keys() - migrations
    assert not stale, f"expectation for a migration that no longer exists: {sorted(stale)}"


@pytest.mark.db
def test_health_confirms_every_migration_on_a_migrated_database(
    client: TestClient, fresh_db: None
) -> None:
    """The fixture applies every migration, so a fully migrated database must
    report every one present — otherwise the expectations are checking for
    something the migrations do not actually create."""
    body = client.get("/health").json()

    assert body["schema_behind"] == []
    assert all(body["schema"].values()), body["schema"]


@pytest.mark.db
def test_cumulative_incidence_never_falls_with_age(client: TestClient, fresh_db: None) -> None:
    """The invariant that caught the bug this test now guards.

    An edit reverted by one hour is still reverted at six. Cumulative incidence
    is therefore monotonically non-decreasing in age, always, for every stratum.

    The first version of the query counted only events observed at or beyond
    each age. An event that tested positive early is never checked again, so it
    left the denominator at every later age — and took its revert out of the
    numerator with it. The published curve fell from 10.62% to 0.76% before
    jumping to 21.27%, which is not a thing that can happen.
    """
    from datetime import UTC, datetime, timedelta

    from bellwether.db import connect

    now = datetime.now(UTC)
    with connect() as conn:
        for revid in range(1, 21):
            conn.execute(
                """
                INSERT INTO landing.rc_events
                    (revid, event_ts, ns, title, is_anon, is_temp, is_minor, is_bot,
                     sampling_stratum, sampling_weight, ingested_at_utc)
                VALUES (%s, %s, 0, 'Page', false, false, false, false,
                        'registered', 2.0, %s)
                """,
                (revid, now - timedelta(days=5), now - timedelta(days=5)),
            )

        # Five reverted at the first checkpoint and never re-checked, exactly
        # like the production data that exposed the bug.
        for revid in range(1, 6):
            conn.execute(
                """
                INSERT INTO outcome.label_checks
                    (revid, checkpoint_seconds, checked_at_utc, age_seconds, had_reverted_tag)
                VALUES (%s, 3600, %s, 3600, true)
                """,
                (revid, now - timedelta(days=4)),
            )
        # The rest observed negative all the way to 48 hours.
        for revid in range(6, 21):
            for checkpoint in (3600, 21600, 86400, 172800):
                conn.execute(
                    """
                    INSERT INTO outcome.label_checks
                        (revid, checkpoint_seconds, checked_at_utc, age_seconds, had_reverted_tag)
                    VALUES (%s, %s, %s, %s, false)
                    """,
                    (revid, checkpoint, now - timedelta(days=3), checkpoint),
                )

    rows = client.get("/maturity").json()["checkpoints"]
    by_stratum: dict[str, list[tuple[int, float]]] = {}
    for row in rows:
        if row["cumulative_incidence"] is not None:
            by_stratum.setdefault(row["stratum"], []).append(
                (row["age_seconds"], row["cumulative_incidence"])
            )

    assert by_stratum, "no curve produced"
    for stratum, points in by_stratum.items():
        points.sort()
        values = [ci for _, ci in points]
        assert values == sorted(values), f"{stratum} incidence fell with age: {points}"

    # And the five early reverts must still be counted at 48 hours.
    at_48h = next(r for r in rows if r["age_seconds"] == 172800)
    assert at_48h["reverted_by"] == 5
    assert at_48h["at_risk"] == 20
