"""The scheduled workflows must parse, and must keep the properties they exist for.

A broken workflow file does not fail loudly — GitHub simply never runs it, and
the first symptom is a gap in the data noticed days later. These assertions are
cheap insurance against the failure mode that produces no error message at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

SCHEDULED = {"ingest.yml": "*/10 * * * *", "label.yml": "*/30 * * * *"}


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(doc: dict[str, Any]) -> dict[str, Any]:
    """Fetch the `on:` block.

    YAML 1.1 parses a bare `on` key as the boolean True, so the obvious
    ``doc["on"]`` returns None and every assertion built on it passes
    vacuously. Reading both spellings is the difference between a test that
    checks the schedule and one that only looks like it does.
    """
    return doc.get("on") or doc.get(True) or {}


@pytest.mark.parametrize("name", sorted(SCHEDULED))
def test_the_schedule_is_what_it_should_be(name: str) -> None:
    crons = [entry["cron"] for entry in _triggers(_load(name))["schedule"]]
    assert crons == [SCHEDULED[name]]


@pytest.mark.parametrize("name", sorted(SCHEDULED))
def test_a_delayed_run_is_not_cancelled_by_its_successor(name: str) -> None:
    """cancel-in-progress would kill a run mid-page.

    Ingestion commits a page and then advances the cursor. Cancelled between
    the two, the rows are committed but the cursor is not — recoverable, but it
    hides that ingestion is falling behind. Queuing is the correct behaviour.
    """
    concurrency = _load(name)["concurrency"]
    assert concurrency["cancel-in-progress"] is False


@pytest.mark.parametrize("name", sorted(SCHEDULED))
def test_the_run_is_bounded(name: str) -> None:
    """NFR-5: a run must finish before its successor is due."""
    job = next(iter(_load(name)["jobs"].values()))
    assert job["timeout-minutes"] <= 10


@pytest.mark.parametrize("name", sorted(SCHEDULED))
def test_production_jobs_do_not_install_test_dependencies(name: str) -> None:
    """pytest and respx have no business running against the live database."""
    steps = next(iter(_load(name)["jobs"].values()))["steps"]
    installs = [s.get("run", "") for s in steps if "pip install" in s.get("run", "")]
    assert installs, "no install step found"
    for cmd in installs:
        assert "requirements-pipeline.txt" in cmd
        assert "requirements-dev.txt" not in cmd


@pytest.mark.parametrize("name", sorted(SCHEDULED))
def test_the_writer_credential_comes_from_a_secret(name: str) -> None:
    job = next(iter(_load(name)["jobs"].values()))
    assert job["env"]["BELLWETHER_DATABASE_URL"] == "${{ secrets.BELLWETHER_DATABASE_URL }}"


def test_the_free_label_path_runs_even_when_ingestion_fails() -> None:
    """It costs no API calls and reads tags already stored, so there is no
    reason to skip it — and a partial ingestion is exactly when the cheapest
    available labels are worth collecting."""
    steps = _load("ingest.yml")["jobs"]["ingest"]["steps"]
    secondary = next(s for s in steps if "label_secondary" in s.get("run", ""))
    assert secondary["if"] == "always()"
