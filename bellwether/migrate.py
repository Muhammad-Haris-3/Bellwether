"""Apply the SQL files in sql/ in filename order.

Every file is idempotent, so this is safe to run against a fresh database or an
existing one, and CI runs it on every push. There is no migration-version
table on purpose: at this size, "re-run everything, and everything is written
to tolerate that" is simpler to reason about than a version ledger that can
itself drift out of step with the files.
"""

from __future__ import annotations

import sys
from pathlib import Path

from bellwether.db import connect

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def apply_all(directory: Path | None = None) -> list[str]:
    directory = directory or SQL_DIR
    applied: list[str] = []
    files = sorted(directory.glob("*.sql"))
    if not files:
        raise RuntimeError(f"No .sql files found in {directory}")

    with connect() as conn:
        for path in files:
            conn.execute(path.read_text(encoding="utf-8").encode("utf-8"))
            applied.append(path.name)
    return applied


def main() -> int:
    for name in apply_all():
        print(f"applied {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
