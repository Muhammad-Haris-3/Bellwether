"""The evidence export: checkable by someone with the repository and nothing else."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bellwether import export, seal
from bellwether.db import connect

MONTH = date(2026, 6, 1)
IN_MONTH = datetime(2026, 6, 15, 12, tzinfo=UTC)


def _evidence(conn: Any, revid: int, score: float = 0.42) -> None:
    conn.execute(
        "INSERT INTO register.predictions"
        " (revid, event_ts, scored_at, model_version, role, score, feature_hash)"
        " VALUES (%s, %s, %s, 'v1', 'champion', %s, 'abc')",
        (revid, IN_MONTH, IN_MONTH + timedelta(minutes=5), score),
    )
    conn.execute(
        "INSERT INTO outcome.labels"
        " (revid, label, label_source, first_observed_at_utc, detection_latency_seconds)"
        " VALUES (%s, true, 'mw_reverted', %s, 3600)",
        (revid, IN_MONTH),
    )


@pytest.fixture
def sealed(fresh_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(seal, "SEALS_DIR", tmp_path / "seals")
    with connect() as conn:
        for revid in (1, 2, 3):
            _evidence(conn, revid)
    seal.seal_month(MONTH)
    return tmp_path / "exports"


@pytest.mark.db
def test_an_export_hashes_to_the_seal_committed_before_it(sealed: Path) -> None:
    manifest = export.export_month(MONTH, exports_dir=sealed)
    assert manifest["files"]["register.predictions.tsv.gz"]["rows"] == 3
    assert export.verify_month("2026-06", exports_dir=sealed) == []


@pytest.mark.db
def test_an_altered_row_is_caught_even_with_the_manifest_rewritten(sealed: Path) -> None:
    """Rewriting the manifest hash is easy; matching a seal committed to git
    before the export existed is not. That is the whole point of checking
    against the seal rather than against the manifest."""
    export.export_month(MONTH, exports_dir=sealed)
    target = sealed / "2026-06"
    path = target / "register.predictions.tsv.gz"
    forged = gzip.decompress(path.read_bytes()).replace(b"0.42", b"0.99", 1)
    path.write_bytes(gzip.compress(forged, mtime=0))

    manifest = json.loads((target / "MANIFEST.json").read_text())
    manifest["files"]["register.predictions.tsv.gz"]["sha256"] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    (target / "MANIFEST.json").write_text(json.dumps(manifest))

    problems = export.verify_month("2026-06", exports_dir=sealed)
    assert any("committed seal" in p for p in problems)


@pytest.mark.db
def test_rows_that_no_longer_match_their_seal_are_not_exported(sealed: Path) -> None:
    """A month altered after sealing must stop the export, and with it the
    pruning — not be published under a label that says it was checked."""
    with connect() as conn:
        _evidence(conn, 4)
    with pytest.raises(export.ExportMismatch):
        export.export_month(MONTH, exports_dir=sealed)
    assert not (sealed / "2026-06" / "MANIFEST.json").exists()


def test_retention_cannot_reach_a_sealed_month_that_was_not_exported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seals, exports = tmp_path / "seals", tmp_path / "exports"
    seals.mkdir()
    monkeypatch.setattr(seal, "SEALS_DIR", seals)
    for month in ("2026-07", "2026-08"):
        (seals / f"{month}.json").write_text("{}")
    (exports / "2026-07").mkdir(parents=True)
    (exports / "2026-07" / "MANIFEST.json").write_text("{}")

    days = export.protected_evidence_days(30, exports_dir=exports)
    cutoff = datetime.now(UTC) - timedelta(days=days)
    assert cutoff < datetime(2026, 8, 1, tzinfo=UTC), "August survives until it is exported"

    (exports / "2026-08").mkdir()
    (exports / "2026-08" / "MANIFEST.json").write_text("{}")
    assert export.protected_evidence_days(30, exports_dir=exports) == 30
