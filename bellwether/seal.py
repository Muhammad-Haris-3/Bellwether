"""Monthly integrity seals (M1-FR-9, M1 §6).

SRS §6.5 promised that predictions and labels would be kept indefinitely,
because they are the evidence the project's claims rest on. Measured against
Neon Free's 0.5 GB that promise cannot be kept. Quietly breaking it would be
the worst option; so would deleting the evidence and saying nothing.

Instead: **seal, then prune.** For each completed month, hash the evidence rows
in a fixed order, write the digest and row counts to `seals/YYYY-MM.json`, and
commit that file to a public repository. The rows may then age out of the
database while the proof that they were not altered persists in git history —
checkable by someone with access to neither the database nor the author.

What a seal proves and what it does not:

  * It proves the rows for a month have not changed since sealing. Recompute
    the digest against what is left and compare.
  * It does **not** prove the rows were right when written. Nothing does. That
    is what the append-only grants and the pre-registration are for.

The digest is computed in Python rather than SQL so it can be reproduced by
anyone with the rows and this file, without a particular database version's
`string_agg` ordering or text encoding.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from bellwether.db import connect
from bellwether.runlog import RunContext, new_run_id, utcnow

JOB = "seal"
SEALS_DIR = Path(__file__).resolve().parent.parent / "seals"

# Each sealed table, with the columns that go into the digest and the order
# rows are read in.
#
# The column list is deliberately explicit rather than SELECT *: adding a
# column later would otherwise change every future digest for reasons unrelated
# to the data, and old seals would appear to disagree with unchanged rows.
SEALED: dict[str, dict[str, str]] = {
    "outcome.labels": {
        "columns": (
            "revid, label, label_source, first_observed_at_utc, "
            "revert_latency_seconds, detection_latency_seconds, revert_revid"
        ),
        "time_column": "first_observed_at_utc",
        "order_by": "revid, label_source",
    },
}


def month_bounds(month: date) -> tuple[datetime, datetime]:
    """Half-open [start, end) in UTC.

    Half-open, so an event at midnight on the first belongs to exactly one
    month. A closed interval would place it in two, and it would be hashed
    into two different seals.
    """
    start = datetime(month.year, month.month, 1, tzinfo=UTC)
    end = datetime(
        month.year + (month.month == 12),
        1 if month.month == 12 else month.month + 1,
        1,
        tzinfo=UTC,
    )
    return start, end


def parse_month(text: str) -> date:
    """Parse YYYY-MM without strptime, which yields a naive datetime."""
    year, month = (int(part) for part in text.split("-", 1))
    return date(year, month, 1)


def previous_month(today: date | None = None) -> date:
    today = today or utcnow().date()
    first = today.replace(day=1)
    return (first - timedelta(days=1)).replace(day=1)


def compute_digest(conn: Any, month: date) -> tuple[str, dict[str, int]]:
    """Hash every sealed row for a month, in a fixed order.

    Values are rendered with repr-free, locale-free formatting and joined with
    a unit separator, so the digest depends on the data and nothing else.
    """
    start, end = month_bounds(month)
    hasher = hashlib.sha256()
    counts: dict[str, int] = {}

    for table, spec in sorted(SEALED.items()):
        hasher.update(f"\x1e{table}\x1e".encode())
        rows = 0
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {spec['columns']} FROM {table} "  # noqa: S608
                f"WHERE {spec['time_column']} >= %s AND {spec['time_column']} < %s "
                f"ORDER BY {spec['order_by']}",
                (start, end),
            )
            for row in cur:
                rows += 1
                line = "\x1f".join("" if value is None else str(value) for value in row.values())
                hasher.update(line.encode("utf-8"))
                hasher.update(b"\x1e")
        counts[table] = rows

    return hasher.hexdigest(), counts


def seal_month(month: date, *, write: bool = True) -> dict[str, Any]:
    run_id = new_run_id()
    with RunContext(run_id, job=JOB) as run_ctx, connect() as conn:
        digest, counts = compute_digest(conn, month)
        payload: dict[str, Any] = {
            "month": month.strftime("%Y-%m"),
            "algorithm": "sha256",
            "digest": digest,
            "row_counts": counts,
            "sealed_at_utc": utcnow().isoformat(),
        }

        if write:
            SEALS_DIR.mkdir(exist_ok=True)
            path = SEALS_DIR / f"{month:%Y-%m}.json"
            path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8")
            payload["path"] = str(path.relative_to(SEALS_DIR.parent))

            with conn.cursor() as cur:
                # A month is sealed once. Re-sealing would let a later digest
                # replace an earlier one, which is precisely the edit the seal
                # exists to make detectable.
                cur.execute(
                    """
                    INSERT INTO outcome.seals (month, row_counts, digest, sealed_by_run)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (month) DO NOTHING
                    """,
                    (month, json.dumps(counts), digest, run_id),
                )
                payload["recorded"] = cur.rowcount == 1

        run_ctx.rows_read = sum(counts.values())

    return payload


def verify_month(month: date) -> dict[str, Any]:
    """Recompute the digest and compare it with the committed seal.

    This is the operation the whole mechanism exists to make possible, and it
    needs nothing but the repository and a database.
    """
    path = SEALS_DIR / f"{month:%Y-%m}.json"
    if not path.exists():
        return {"month": f"{month:%Y-%m}", "status": "no seal committed"}

    sealed = json.loads(path.read_text("utf-8"))
    with connect() as conn:
        digest, counts = compute_digest(conn, month)

    matches = digest == sealed["digest"]
    return {
        "month": f"{month:%Y-%m}",
        "status": "intact" if matches else "MISMATCH",
        "sealed_digest": sealed["digest"],
        "current_digest": digest,
        "sealed_counts": sealed["row_counts"],
        "current_counts": counts,
        # A pruned month legitimately holds fewer rows than it was sealed with.
        # That is not tampering, and the seal cannot tell the two apart — which
        # is why verification is run BEFORE pruning, and why the counts are
        # published alongside the digest rather than hidden behind a boolean.
        "note": None if matches else "rows differ from the sealed set",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", help="YYYY-MM. Defaults to the previous complete month.")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Compute but write nothing")
    args = parser.parse_args()

    month = parse_month(args.month) if args.month else previous_month()

    if args.verify:
        print(json.dumps(verify_month(month), indent=2, default=str))
        return 0

    payload = seal_month(month, write=not args.dry_run)
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
