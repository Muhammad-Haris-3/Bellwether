"""The run log (FR-5).

Every scheduled job runs inside a :class:`RunContext`. A job that fails still
writes its row — the log is most useful precisely when things went wrong, so
recording only successes would be exactly the wrong way round.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import TracebackType
from typing import Literal

from bellwether.db import connect


def new_run_id() -> uuid.UUID:
    return uuid.uuid4()


def utcnow() -> datetime:
    return datetime.now(UTC)


class RunContext:
    """Opens and closes one ``landing.run_log`` row.

    Usage::

        with RunContext(new_run_id(), job="ingest") as run:
            run.api_calls += 1
            run.rows_written += written
    """

    def __init__(
        self,
        run_id: uuid.UUID,
        *,
        job: str,
        window_from: datetime | None = None,
        window_to: datetime | None = None,
    ) -> None:
        self.run_id = run_id
        self.job = job
        self.window_from = window_from
        self.window_to = window_to
        self.api_calls = 0
        self.rows_read = 0
        self.rows_written = 0
        self.partial = False
        self._run_log_id: int | None = None

    def __enter__(self) -> RunContext:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO landing.run_log
                    (run_id, job, window_from_utc, window_to_utc, started_at_utc, status)
                VALUES (%s, %s, %s, %s, %s, 'running')
                RETURNING run_log_id
                """,
                (self.run_id, self.job, self.window_from, self.window_to, utcnow()),
            )
            row = cur.fetchone()
            self._run_log_id = row["run_log_id"] if row else None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        if exc_type is None:
            status = "partial" if self.partial else "success"
            error_class = error_detail = None
        else:
            status = "failed"
            error_class = exc_type.__name__
            error_detail = str(exc)[:2000]

        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE landing.run_log
                   SET finished_at_utc = %s,
                       api_calls       = %s,
                       rows_read       = %s,
                       rows_written    = %s,
                       status          = %s,
                       error_class     = %s,
                       error_detail    = %s,
                       window_from_utc = COALESCE(%s, window_from_utc),
                       window_to_utc   = COALESCE(%s, window_to_utc)
                 WHERE run_log_id = %s
                """,
                (
                    utcnow(),
                    self.api_calls,
                    self.rows_read,
                    self.rows_written,
                    status,
                    error_class,
                    error_detail,
                    self.window_from,
                    self.window_to,
                    self._run_log_id,
                ),
            )
        # Never suppress. A failed job must fail the workflow step, or a broken
        # pipeline looks healthy from the outside — which is the failure mode
        # that costs a week before anyone notices.
        return False
