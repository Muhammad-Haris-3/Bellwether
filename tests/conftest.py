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
def db_url() -> str:
    url = os.environ.get("BELLWETHER_DATABASE_URL", "")
    if not url:
        pytest.skip("BELLWETHER_DATABASE_URL not set")
    return url


@pytest.fixture
def fresh_db(db_url: str) -> Iterator[None]:
    """Apply the schema and truncate the M0 tables.

    Truncation runs as the owner, not as bellwether_writer — the writer role is
    specifically forbidden from doing this, and a fixture that needed the
    privilege it is meant to be testing the absence of would be circular.
    """
    from bellwether.db import connect
    from bellwether.migrate import apply_all

    apply_all()
    with connect() as conn:
        conn.execute(
            "TRUNCATE outcome.labels, outcome.label_checks, "
            "landing.rc_events, landing.cursors, landing.run_log RESTART IDENTITY CASCADE"
        )
    yield
