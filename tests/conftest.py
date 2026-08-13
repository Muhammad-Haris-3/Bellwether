from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from bellwether.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Settings are cached with lru_cache; tests that patch env must not leak."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db_url(monkeypatch: pytest.MonkeyPatch) -> str:
    """A database the tests are allowed to destroy.

    Deliberately a SEPARATE variable from BELLWETHER_DATABASE_URL. The db tests
    truncate every table, and pointing them at the working database wipes
    hours of ingested history — which is exactly what happened once, silently,
    because a green test run and a wiped database look identical from the
    terminal.

    Skipping when it is unset is the safe default: a developer who has not
    opted in gets no db coverage rather than an empty database.
    """
    url = os.environ.get("BELLWETHER_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("BELLWETHER_TEST_DATABASE_URL not set (db tests are destructive)")

    if url == os.environ.get("BELLWETHER_DATABASE_URL"):
        pytest.fail(
            "BELLWETHER_TEST_DATABASE_URL is the same as BELLWETHER_DATABASE_URL. "
            "The db tests truncate every table; they must not point at the "
            "working database."
        )

    # Every module under test resolves its connection through Settings, so
    # redirecting here covers the whole suite rather than each call site.
    monkeypatch.setenv("BELLWETHER_DATABASE_URL", url)
    get_settings.cache_clear()
    return url


@pytest.fixture
def fresh_db(db_url: str) -> Iterator[None]:
    """Apply the schema and truncate the M0 tables.

    Truncation runs as the owner, not as bellwether_writer — the writer role is
    specifically forbidden from doing this, and a fixture needing the very
    privilege it exists to prove absent would be circular.
    """
    from bellwether.db import connect
    from bellwether.migrate import apply_all

    apply_all()
    with connect() as conn:
        # Every table in every schema this project owns, discovered rather
        # than listed — and the SCHEMAS are discovered too.
        #
        # This list has now rotted three times. First tag_names, then
        # gap_attempts, each added to the schema without being added here. The
        # fix then was to discover tables instead of naming them — but the
        # schemas stayed hard-coded, so when M3 added `register` the whole
        # schema leaked between tests and eight of them failed on a unique
        # constraint that had nothing to do with what they were testing.
        #
        # Excluding the system schemas by name is the only stable version of
        # this: new project schemas are included automatically, and there is
        # nothing left to forget to update.
        #
        # quote_ident rather than format('%I.%I', ...): psycopg reads a bare
        # per-cent as the start of a placeholder, even inside SQL that is only
        # building a string.
        tables = conn.execute(
            """
            SELECT quote_ident(table_schema) || '.' || quote_ident(table_name) AS t
              FROM information_schema.tables
             WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'public')
               AND table_schema NOT LIKE 'pg_%%'
               AND table_type = 'BASE TABLE'
            """
        ).fetchall()
        if tables:
            names = ", ".join(row["t"] for row in tables)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")  # noqa: S608
    yield
