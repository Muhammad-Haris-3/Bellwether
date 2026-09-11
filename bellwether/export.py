"""A copy of the evidence that anyone can check without the database.

A seal proves a month's rows were not altered, but only to someone who still
has the rows — and the rows are pruned after thirty days. On 2026-09-10 the
database filled and Neon paused the project, and for the three weeks until the
allowance reset nobody could read any of it, the author included. The proof
existed; the thing it was proof of was out of reach.

So each sealed month is exported, before retention may touch it, into
`exports/YYYY-MM/`: the sealed rows, rendered by the same function the seal
hashed them with, plus a snapshot of the published results. The files are
committed to the public repository.

**Why it can be checked rather than trusted.** The seal digest was committed to
git before the export existed. `--verify` recomputes that digest from the export
alone — no database, no credentials — and compares. A single altered row, added
row or dropped row changes it. The seal fixes the rows; git fixes when the seal
was made.

The results snapshot (metrics history, decisions, models) has no seal predating
it. Its proof starts at export: the SHA-256 in MANIFEST.json, committed publicly,
fixes the files from then on and says nothing about before.

    python -m bellwether.export                      # every sealed month not yet exported
    python -m bellwether.export --verify             # every exported month, offline
    python -m bellwether.export --verify 2026-08
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from bellwether import seal
from bellwether.config import get_settings
from bellwether.db import connect
from bellwether.runlog import utcnow
from bellwether.usage import record_on_exit

EXPORTS_DIR = seal.SEALS_DIR.parent / "exports"

# What the site publishes, taken whole each time a month is exported. Reviewer
# verdicts (app.human_labels) are left out: they carry who judged, and a public
# repository is the wrong place for that.
SNAPSHOT = (
    "outcome.prediction_metrics",
    "outcome.calibration_bins",
    "outcome.liftwing_scores",
    "outcome.label_agreement",
    "outcome.evaluations",
    "register.model_registry",
    "register.reproductions",
    "decide.model_decisions",
    "decide.champion_history",
    "decide.trigger_evaluations",
)


class ExportMismatch(RuntimeError):
    """The rows in the database no longer hash to the committed seal."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _gzip(text: str) -> bytes:
    # mtime=0, so the same rows always give the same bytes and the same hash.
    return gzip.compress(text.encode("utf-8"), mtime=0)


def _sealed_file(conn: Any, month: date, table: str) -> tuple[bytes, list[str]]:
    """Tab-separated for people, reversible to the hashed line for the check."""
    header = "\t".join(c.strip() for c in seal.SEALED[table]["columns"].split(","))
    lines = []
    for line in seal.sealed_lines(conn, month, table):
        if "\t" in line or "\n" in line:
            raise ExportMismatch(f"{table} has a value the export cannot carry exactly: {line!r}")
        lines.append(line)
    body = "".join(line.replace("\x1f", "\t") + "\n" for line in lines)
    return _gzip(header + "\n" + body), lines


def _snapshot_file(conn: Any, table: str) -> tuple[bytes, int]:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {table} ORDER BY 1")  # noqa: S608 - constant names
        writer.writerow([col.name for col in cur.description or ()])
        rows = 0
        for row in cur:
            writer.writerow(["" if v is None else str(v) for v in row.values()])
            rows += 1
    return _gzip(out.getvalue()), rows


def export_month(month: date, *, exports_dir: Path = EXPORTS_DIR) -> dict[str, Any]:
    """Refuses rather than writes when the rows no longer match the seal:
    publishing that set under a verifiable label would be the one thing worse
    than publishing nothing."""
    name = f"{month:%Y-%m}"
    sealed = json.loads((seal.SEALS_DIR / f"{name}.json").read_text("utf-8"))
    files: dict[str, bytes] = {}
    manifest: dict[str, Any] = {"month": name, "files": {}}

    with connect() as conn:
        lines: dict[str, list[str]] = {}
        for table in sorted(seal.SEALED):
            files[f"{table}.tsv.gz"], lines[table] = _sealed_file(conn, month, table)
            manifest["files"][f"{table}.tsv.gz"] = {"rows": len(lines[table])}
        digest, _ = seal.digest_of(lines)
        if digest != sealed["digest"]:
            raise ExportMismatch(
                f"{name}: the database's rows hash to {digest}, the committed seal says "
                f"{sealed['digest']}. Nothing written. Pruned or altered rows cannot be "
                "exported as the sealed set."
            )
        for table in SNAPSHOT:
            data, rows = _snapshot_file(conn, table)
            files[f"results/{table}.csv.gz"] = data
            manifest["files"][f"results/{table}.csv.gz"] = {"rows": rows}

    target = exports_dir / name
    for relative, data in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        manifest["files"][relative]["sha256"] = _sha256(data)
    manifest |= {
        "seal": {"file": f"seals/{name}.json", "digest": sealed["digest"]},
        "exported_at_utc": utcnow().isoformat(),
        "code_commit": get_settings().build_id,
    }
    # Written last: its presence is what tells retention the month is safe.
    (target / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def verify_month(name: str, *, exports_dir: Path = EXPORTS_DIR) -> list[str]:
    """Every problem found, offline. An empty list means the export is intact
    and hashes to the seal that was committed before it."""
    target = exports_dir / name
    manifest = json.loads((target / "MANIFEST.json").read_text("utf-8"))
    problems = [
        f"{relative}: sha256 differs from the manifest"
        for relative, meta in manifest["files"].items()
        if _sha256((target / relative).read_bytes()) != meta["sha256"]
    ]
    lines = {
        table: [
            line.replace("\t", "\x1f")
            for line in gzip.decompress((target / f"{table}.tsv.gz").read_bytes())
            .decode("utf-8")
            .splitlines()[1:]
        ]
        for table in seal.SEALED
    }
    digest, _ = seal.digest_of(lines)
    committed = json.loads((seal.SEALS_DIR / f"{name}.json").read_text("utf-8"))["digest"]
    if digest != committed:
        problems.append(f"rows hash to {digest}, the committed seal is {committed}")
    return problems


def unexported(exports_dir: Path = EXPORTS_DIR) -> list[str]:
    """Sealed months with no export yet, oldest first."""
    return [
        path.stem
        for path in sorted(seal.SEALS_DIR.glob("*.json"))
        if not (exports_dir / path.stem / "MANIFEST.json").exists()
    ]


def protected_evidence_days(configured: int, *, exports_dir: Path = EXPORTS_DIR) -> int:
    """The evidence window retention may use: never short enough to reach a
    sealed month that has not been exported. Seal, export, then prune."""
    pending = unexported(exports_dir)
    if not pending:
        return configured
    start, _ = seal.month_bounds(seal.parse_month(pending[0]))
    return max(configured, (utcnow() - start).days + 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", nargs="*", metavar="YYYY-MM", default=None)
    args = parser.parse_args()

    if args.verify is not None:
        months = args.verify or sorted(p.name for p in EXPORTS_DIR.glob("*") if p.is_dir())
        failed = False
        for name in months:
            problems = verify_month(name)
            print(f"export {name}: {'intact, matches its seal' if not problems else 'FAILED'}")
            for problem in problems:
                print(f"  {problem}")
            failed |= bool(problems)
        return 1 if failed else 0

    record_on_exit("export")
    for name in unexported():
        manifest = export_month(seal.parse_month(name))
        rows = {k: v["rows"] for k, v in manifest["files"].items() if not k.startswith("results/")}
        print(f"export {name}: {rows}, matches seal {manifest['seal']['digest'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
