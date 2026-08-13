"""Database access.

Thin helpers over psycopg3. No ORM: every query in this project is written to
be readable as SQL, because the SQL is part of what is being demonstrated.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import DictRow, dict_row

from bellwether.config import get_settings

# Every connection in this project uses dict_row, so queries read like the SQL
# they contain rather than like tuple indexing. Spelling the row type out here
# keeps that visible to the type checker instead of degrading to Any.
Conn = psycopg.Connection[DictRow]


@contextmanager
def connect(url: str | None = None, *, readonly: bool = False) -> Iterator[Conn]:
    """Open a connection. Commits on clean exit, rolls back on exception."""
    settings = get_settings()
    dsn = url or (settings.serving_url if readonly else settings.database_url)
    if not dsn:
        raise RuntimeError(
            "No database URL configured. Set BELLWETHER_DATABASE_URL "
            "(and BELLWETHER_READONLY_DATABASE_URL for the API)."
        )
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        if readonly:
            conn.read_only = True

        # Every connection speaks UTC, wherever it runs.
        #
        # This project subtracts timestamps that originate on three different
        # machines — MediaWiki's servers, a GitHub runner, and Postgres itself —
        # to produce detection and revert latencies. Those latencies are the
        # input to the maturity window, which decides which predictions are
        # allowed to count. A session timezone difference would not raise an
        # error; it would shift a survival curve by a few hours and change a
        # published number, which is far worse.
        conn.execute("SET TIME ZONE 'UTC'")

        yield conn


@contextmanager
def advisory_lock(conn: Conn, key: int) -> Iterator[bool]:
    """Try to take a session-level advisory lock; yield whether we got it.

    Scheduled workflows can overlap: GitHub's cron is best-effort and a delayed
    run can start while its predecessor is still going. Two ingestion runs on
    the same window are not corrupting — ON CONFLICT DO NOTHING sees to that —
    but they would double the API call rate against a free public service, and
    they would race on the cursor. Cheaper to refuse the second one.
    """
    got = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (key,)).fetchone()
    acquired = bool(got and got["locked"])
    try:
        yield acquired
    finally:
        if acquired:
            conn.execute("SELECT pg_advisory_unlock(%s)", (key,))


def fetch_all(
    sql: str, params: Sequence[Any] | dict[str, Any] | None = None, *, readonly: bool = False
) -> list[dict[str, Any]]:
    with connect(readonly=readonly) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(
    sql: str, params: Sequence[Any] | dict[str, Any] | None = None, *, readonly: bool = False
) -> dict[str, Any] | None:
    with connect(readonly=readonly) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(sql: str, params: Sequence[Any] | dict[str, Any] | None = None) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount
