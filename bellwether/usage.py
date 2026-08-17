"""Database transfer accounting.

    python -m bellwether.usage --report     # period-to-date against the budget
    python -m bellwether.usage --check      # non-zero past the decline threshold

WHY THIS EXISTS

NFR-4 caps storage, and bellwether.retention reports against it daily. Nothing
capped data transfer, which is a second Neon Free allowance with a worse
failure mode: storage exhaustion refuses writes, transfer exhaustion refuses
connections — so the pipeline, the watchdog and the API stop together.

GridCast, on the same plan and the same architecture, spent its transfer
allowance on 2026-08-17 and stopped completely. Its append-only register stopped
growing and could not be exported, because exporting is reading. Nothing was
counting, so the first signal was a refused connection.

This is the counter that project did not have, ported before Bellwether needs
it. Roughly 227 scheduled jobs run here every day.

WHAT THE NUMBER IS

An estimate. It measures the width of the values returned, not the bytes on the
wire: it sees neither protocol framing nor TLS nor compression, and it will
disagree with Neon's own figure by a margin that varies with the query.

It is reported as an estimate everywhere it appears, and deliberately NOT tuned
against the console figure to look more accurate than it is. Its job is to make
a tenfold regression obvious on the day it lands, and a consistent estimate does
that as well as an exact one. Deciding how much allowance remains is the one use
it does not support.
"""

from __future__ import annotations

import argparse
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from bellwether.config import get_settings

# Neon Free's monthly data transfer allowance. A published figure about somebody
# else's product, so it is stated once and read from here rather than repeated
# in comments that will not be updated together.
FREE_TIER_BUDGET_BYTES = 5 * 1024**3

# Where a human should look, and where deferrable work should stand down. Two
# thresholds because they are two decisions. 80% matches the storage threshold
# in bellwether.retention, so the two budgets warn at the same place.
WARN_FRACTION = 0.80
DECLINE_FRACTION = 0.90

# Per-row protocol overhead: a DataRow header plus a length prefix per field.
# Without it the estimate understates badly on narrow rows, which is exactly the
# shape of this project's hottest reads.
ROW_OVERHEAD_BYTES = 7
FIELD_OVERHEAD_BYTES = 4


def value_width(value: Any) -> int:
    """Estimated wire width of one value, from the value itself.

    Measured rather than assumed wherever the type is variable-width. This
    project's landing tables hold both tag arrays and short status words, and a
    flat per-column cost would make a read of one indistinguishable from a read
    of the other — losing precisely the contrast worth watching.
    """
    if value is None:
        return FIELD_OVERHEAD_BYTES
    if isinstance(value, bool):
        return FIELD_OVERHEAD_BYTES + 1
    if isinstance(value, int):
        return FIELD_OVERHEAD_BYTES + 8
    if isinstance(value, float):
        return FIELD_OVERHEAD_BYTES + 8
    if isinstance(value, bytes | bytearray | memoryview):
        return FIELD_OVERHEAD_BYTES + len(value)
    if isinstance(value, str):
        return FIELD_OVERHEAD_BYTES + len(value.encode("utf-8"))
    if isinstance(value, datetime | date | time):
        return FIELD_OVERHEAD_BYTES + 8
    if isinstance(value, Decimal):
        return FIELD_OVERHEAD_BYTES + len(str(value))
    if isinstance(value, uuid.UUID):
        return FIELD_OVERHEAD_BYTES + 16
    return FIELD_OVERHEAD_BYTES + len(str(value).encode("utf-8"))


class Meter:
    """Running total for one process.

    Process-scoped rather than global-with-reset: every job here is a short
    `python -m` invocation, so the process boundary and the unit of work are the
    same thing. A meter needing a manual reset would eventually be read after
    somebody forgot.
    """

    def __init__(self) -> None:
        self.queries = 0
        self.rows = 0
        self.bytes_estimated = 0

    def record(self, rows: Sequence[Any] | Iterable[Any]) -> None:
        materialised = list(rows)
        self.queries += 1
        self.rows += len(materialised)
        for row in materialised:
            values = row.values() if isinstance(row, dict) else row
            self.bytes_estimated += ROW_OVERHEAD_BYTES + sum(value_width(v) for v in values)

    def summary(self) -> str:
        plural = "y" if self.queries == 1 else "ies"
        return (
            f"{self.rows:,} rows over {self.queries:,} quer{plural}, "
            f"~{human_bytes(self.bytes_estimated)}"
        )


METER = Meter()


def human_bytes(count: int | float) -> str:
    step = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if step < 1024 or unit == "GB":
            return f"{step:,.0f} B" if unit == "B" else f"{step:,.1f} {unit}"
        step /= 1024
    return f"{step:,.1f} GB"


def period_start(now: datetime | None = None) -> datetime:
    """The instant the current billing period began."""
    now = now or datetime.now(UTC)
    day = max(1, min(28, get_settings().billing_period_day))

    if now.day >= day:
        return now.replace(day=day, hour=0, minute=0, second=0, microsecond=0)

    month = now.month - 1 or 12
    year = now.year - 1 if month == 12 else now.year
    return now.replace(year=year, month=month, day=day, hour=0, minute=0, second=0, microsecond=0)


def record_run(job: str, run_id: uuid.UUID | None = None) -> None:
    """Append this process's total. Never raises.

    Accounting that can fail the job it accounts for is worse than none, and the
    case is unmissable here: this table lives in the database whose exhaustion
    it exists to predict, so it will be unreachable at exactly the moment it is
    most interesting.
    """
    from bellwether.db import connect  # local: bellwether.db imports this module

    if METER.queries == 0:
        return

    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO landing.db_transfer
                    (run_id, job, queries, rows_returned, bytes_estimated, code_commit)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    job,
                    METER.queries,
                    METER.rows,
                    METER.bytes_estimated,
                    get_settings().build_id,
                ),
            )
    except Exception as exc:  # noqa: BLE001 — accounting must not break the job
        print(f"usage: could not record transfer ({type(exc).__name__}: {exc})")


def record_on_exit(job: str, run_id: uuid.UUID | None = None) -> None:
    """Record this process's transfer when it ends, however it ends.

    Registered at the start of a job rather than called at the end, because the
    runs worth measuring include the ones that raise. A job that died halfway
    through a large read still spent it, and accounting only clean exits would
    show the allowance draining into nothing.
    """
    import atexit

    atexit.register(record_run, job, run_id)


def period_total() -> tuple[int, int, int]:
    """(bytes, rows, runs) recorded since the billing period began."""
    from bellwether.db import connect

    with connect(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT coalesce(sum(bytes_estimated), 0) AS bytes_estimated,
                   coalesce(sum(rows_returned), 0)   AS rows_returned,
                   count(*)                          AS runs
              FROM landing.db_transfer
             WHERE recorded_at_utc >= %s
            """,
            (period_start(),),
        )
        row = cur.fetchone()

    if not row:
        return 0, 0, 0
    return int(row["bytes_estimated"]), int(row["rows_returned"]), int(row["runs"])


def by_job(limit: int = 12) -> list[dict[str, Any]]:
    """The heaviest jobs this period.

    The total says whether there is a problem; this says where it is. Aggregated
    in SQL and returned as a dozen rows, because a module about the cost of
    reading should not pull a period of rows to a runner to add them up.
    """
    from bellwether.db import connect

    with connect(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT job,
                   count(*)             AS runs,
                   sum(bytes_estimated) AS bytes_estimated,
                   sum(rows_returned)   AS rows_returned
              FROM landing.db_transfer
             WHERE recorded_at_utc >= %s
             GROUP BY job
             ORDER BY sum(bytes_estimated) DESC
             LIMIT %s
            """,
            (period_start(), limit),
        )
        return cur.fetchall()


def budget_status() -> dict[str, Any]:
    """Period-to-date usage against the allowance."""
    used, rows, runs = period_total()
    budget = get_settings().transfer_budget_bytes or FREE_TIER_BUDGET_BYTES
    fraction = used / budget if budget else 0.0

    if fraction >= DECLINE_FRACTION:
        state = "over"
    elif fraction >= WARN_FRACTION:
        state = "warn"
    else:
        state = "ok"

    return {
        "period_start_utc": period_start().isoformat(),
        "bytes_estimated": used,
        "bytes_estimated_human": human_bytes(used),
        "budget_bytes": budget,
        "budget_human": human_bytes(budget),
        "fraction_used": round(fraction, 4),
        "rows_returned": rows,
        "runs_recorded": runs,
        "state": state,
        "estimate_note": (
            "Estimated from returned value widths, not measured on the wire. It will "
            "disagree with the provider's figure; it exists to expose a regression, "
            "not to report remaining allowance."
        ),
    }


def should_decline(job: str) -> bool:
    """Whether deferrable work should stand down this run.

    Only ever consulted by jobs whose output survives being a day late.
    Ingestion, scoring and the register never call this: an event not ingested
    is gone from the wiki's recentchanges window within days, and a prediction
    not scored is evidence permanently missing. Spending the last of an
    allowance on those is the correct trade.
    """
    try:
        status = budget_status()
    except Exception as exc:  # noqa: BLE001 — an unreadable budget is not a reason to stop
        print(f"usage: could not read the budget ({type(exc).__name__}); proceeding")
        return False

    if status["state"] != "over":
        return False

    print(
        f"::warning title=Transfer budget::{job} is standing down. Estimated "
        f"{status['bytes_estimated_human']} of {status['budget_human']} used since "
        f"{status['period_start_utc'][:10]} ({status['fraction_used']:.0%}). This job "
        "tolerates being a day late; ingestion and scoring do not and are unaffected."
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", help="Print period-to-date usage")
    parser.add_argument(
        "--check", action="store_true", help="Exit non-zero past the decline threshold"
    )
    args = parser.parse_args()

    status = budget_status()
    print(f"usage: period began {status['period_start_utc'][:10]}")
    print(
        f"usage: ~{status['bytes_estimated_human']} of {status['budget_human']} "
        f"({status['fraction_used']:.1%}) over {status['runs_recorded']:,} runs"
    )
    print(f"usage: {status['rows_returned']:,} rows returned")

    rows = by_job()
    if rows:
        print("usage: heaviest jobs this period")
        for row in rows:
            print(
                f"  {row['job']:<22} {human_bytes(int(row['bytes_estimated'])):>10}"
                f"  {int(row['rows_returned']):>12,} rows  {row['runs']:>5} runs"
            )

    print(f"usage: {status['estimate_note']}")

    if status["state"] == "over":
        # Loud, because the failure mode past this point is Neon refusing
        # connections, which surfaces as every job failing at once for reasons
        # that look nothing like a transfer problem.
        print(
            f"::error title=Transfer budget::Past {DECLINE_FRACTION:.0%} of the "
            "allowance. Deferrable jobs stand down; ingestion and scoring continue."
        )
        return 1 if args.check else 0

    if status["state"] == "warn":
        print(
            f"::warning title=Transfer budget::Past {WARN_FRACTION:.0%} of the "
            "allowance with the period still open."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
