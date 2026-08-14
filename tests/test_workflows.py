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

SCHEDULED = {
    "ingest.yml": "*/10 * * * *",
    "label.yml": "*/30 * * * *",
    "maintain.yml": "17 3 * * *",
    "reconcile.yml": "23 4 * * *",
    "reproduce.yml": "19 5 * * *",
    "metrics.yml": "7 */6 * * *",
}


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
    """pytest and respx have no business running against the live database.

    Stated as a ban on the dev requirements rather than a demand for one
    specific file. The first version required every install to name
    requirements-pipeline.txt, which failed the moment scoring needed its own
    pinned model dependencies — a true rule enforced by a proxy that was not.
    """
    steps = next(iter(_load(name)["jobs"].values()))["steps"]
    installs = [s.get("run", "") for s in steps if "pip install" in s.get("run", "")]
    assert installs, "no install step found"
    for cmd in installs:
        assert "requirements-dev.txt" not in cmd
        assert "pytest" not in cmd
        # Every install comes from a pinned file, never a bare package name.
        assert "-r " in cmd, cmd


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


def test_maintenance_seals_before_it_prunes() -> None:
    """Order is the guard, not decoration.

    The pruning function refuses evidence from an unsealed month, so a failure
    while sealing stops the deletion instead of proceeding without the proof it
    was supposed to leave behind. If retention ever ran first, that refusal
    would be the only thing standing between a bug and unrecoverable loss.
    """
    steps = [s.get("name", "") for s in _load("maintain.yml")["jobs"]["maintain"]["steps"]]
    verify = next(i for i, n in enumerate(steps) if "Verify" in n)
    seal = next(i for i, n in enumerate(steps) if n == "Seal the previous month")
    commit = next(i for i, n in enumerate(steps) if n == "Commit the seal")
    prune = next(i for i, n in enumerate(steps) if n == "Retention")

    assert verify < seal < commit < prune


def test_retention_does_not_delete_on_a_manual_run_by_default() -> None:
    """`inputs.apply` is the empty string on a scheduled run, so a negative
    test on it would delete by accident on exactly the trigger that matters."""
    steps = _load("maintain.yml")["jobs"]["maintain"]["steps"]
    script = next(s["run"] for s in steps if s.get("name") == "Retention")

    assert 'github.event_name }}" = "schedule"' in script
    assert 'inputs.apply }}" = "true"' in script


def test_maintenance_can_write_to_the_repository() -> None:
    """A seal in a database is worth little; in public git history it is the
    whole mechanism."""
    assert _load("maintain.yml")["permissions"]["contents"] == "write"


def test_training_and_evaluation_are_never_scheduled() -> None:
    """Both are decision procedures, not maintenance.

    A model retrained on a timer is a model chosen by whichever run happened to
    look best, and an evaluation on a timer invites re-rolling until a result
    is acceptable. Both are exactly the selection the pre-registration exists
    to prevent, so both are manual until M5 introduces retraining on a trigger
    with promotion by a rule fixed beforehand.
    """
    for name in ("train.yml", "evaluate.yml"):
        assert "schedule" not in _triggers(_load(name)), name
        assert "workflow_dispatch" in _triggers(_load(name)), name


def test_the_scorer_runs_straight_after_ingestion() -> None:
    """The lag between an edit and its score is one polling interval plus
    whatever the scheduler adds. Putting scoring in its own workflow would add
    another interval for nothing."""
    steps = [s.get("name", "") for s in _load("ingest.yml")["jobs"]["ingest"]["steps"]]
    ingest = next(i for i, n in enumerate(steps) if n == "Ingest recent changes")
    scoring = next(i for i, n in enumerate(steps) if n == "Score newly ingested edits")
    assert ingest < scoring
