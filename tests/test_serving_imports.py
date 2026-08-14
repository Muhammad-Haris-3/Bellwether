"""What the serving container is allowed to import (NFR-8, M6).

`test_serving_requirements_carry_no_http_client` checks the requirements FILE.
That is a claim about what is installed, and it says nothing about what the code
reaches for — which is how a deploy died on:

    File "/app/api/review.py", line 27, in <module>
        from bellwether import metrics
    File "/app/bellwether/metrics.py", line 35, in <module>
        import numpy as np
    ModuleNotFoundError: No module named 'numpy'

One import for one constant took the entire modelling stack into the serving
image's import graph. Every test passed: the dev environment has numpy, so
nothing local could tell the difference between "the API imports this" and "the
API can import this".

So this walks the graph statically instead. It reads the API's modules, follows
every `bellwether.*` import transitively, and fails if anything reachable needs
a package the serving image does not install.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
API = REPO / "api"
PACKAGE = REPO / "bellwether"

# Not installed in the serving image, by design. numpy and scikit-learn are the
# modelling stack; httpx and tenacity talk to MediaWiki, and "the API cannot
# write" stops being true the moment the serving image contains the client.
FORBIDDEN_IN_SERVING = {
    "numpy",
    "sklearn",
    "scipy",
    "pandas",
    "httpx",
    "tenacity",
    "requests",
}


def _imports(path: Path) -> set[str]:
    """Top-level module names this file imports, at module scope only.

    Module scope on purpose: an import inside a function is paid when that
    function runs, and the failure this test exists for happened at import
    time — the container never got as far as serving a request.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        # Skip anything nested in a function; only module-level imports load on
        # container start.
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module.startswith("bellwether"):
                found.update(f"bellwether.{alias.name}" for alias in node.names)
                found.add(node.module)
            else:
                found.add(node.module.split(".")[0])
    return found


def _reachable_from_api() -> dict[str, set[str]]:
    """Every bellwether module the API pulls in, and what each one imports."""
    graph: dict[str, set[str]] = {}
    queue = [path for path in API.glob("*.py")]
    seen: set[Path] = set()

    while queue:
        path = queue.pop()
        if path in seen or not path.exists():
            continue
        seen.add(path)
        names = _imports(path)
        graph[str(path.relative_to(REPO)).replace("\\", "/")] = names

        for name in names:
            if not name.startswith("bellwether"):
                continue
            candidate = PACKAGE / f"{name.split('.')[-1]}.py"
            if candidate.exists():
                queue.append(candidate)
    return graph


def test_the_serving_image_never_reaches_the_modelling_stack() -> None:
    graph = _reachable_from_api()
    offenders = {
        module: sorted(names & FORBIDDEN_IN_SERVING)
        for module, names in graph.items()
        if names & FORBIDDEN_IN_SERVING
    }
    assert not offenders, (
        "the API's import graph reaches packages the serving image does not "
        f"install, so the container will not start: {offenders}"
    )


def test_the_walk_actually_follows_bellwether_imports() -> None:
    """The guard against this test passing vacuously.

    If the traversal stopped at `api/`, it would find nothing forbidden and
    report success while the real failure — a pipeline module imported one level
    down — sat untouched. It must be visiting the package.
    """
    graph = _reachable_from_api()
    visited = {module for module in graph if module.startswith("bellwether/")}
    assert visited, "the walk never left api/, so it is checking nothing"
    assert "bellwether/config.py" in visited


@pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_IN_SERVING))
def test_the_forbidden_list_is_not_silently_empty(forbidden: str) -> None:
    """Each name is one the pipeline genuinely uses. A list of packages nothing
    imports would pass forever while protecting nothing."""
    used = any(
        forbidden in _imports(path) for path in PACKAGE.glob("*.py") if path.name != "maturity.py"
    )
    assert used or forbidden in {"scipy", "pandas", "requests"}, (
        f"{forbidden} is on the forbidden list but nothing in the pipeline "
        "imports it — either it is obsolete or the check is not doing what it says"
    )
