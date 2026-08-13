"""The model registry (M3-FR-14 to FR-16).

The artifact lives in git; the registry row records what it is. A promotion
should be verifiable by someone holding the repository and no database access
at all, and a blob in a table they cannot reach is not evidence to them.

**The hash is checked before every load.** A pickled scikit-learn model is tied
to its library version, and a mismatch between the artifact on disk and the one
the registry describes would not raise — it would score differently. Checking
the digest turns a silent behaviour change into a refusal.

That fragility is a recorded deferral rather than a solved problem (M3 §5). The
version is pinned, the hash is verified, and a portable format would still be
better.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"


class ArtifactMismatch(RuntimeError):
    """The artifact on disk is not the one the registry describes."""


def artifact_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def card_path(model_version: str) -> Path:
    return MODELS_DIR / f"{model_version}.json"


def model_path(model_version: str) -> Path:
    return MODELS_DIR / f"{model_version}.pkl"


def write_card(model_version: str, card: dict[str, Any]) -> Path:
    """The metric card committed beside the artifact.

    Sorted keys and a trailing newline so a re-registration of identical
    content produces an identical file — a diff should mean something changed,
    not that the dictionary iterated differently.
    """
    MODELS_DIR.mkdir(exist_ok=True)
    path = card_path(model_version)
    path.write_text(json.dumps(card, indent=2, sort_keys=True, default=str) + "\n", "utf-8")
    return path


def load_card(model_version: str) -> dict[str, Any]:
    return json.loads(card_path(model_version).read_text("utf-8"))


def verify(model_version: str, expected_sha256: str) -> Path:
    """Return the artifact path, or refuse.

    Called before loading, never after. A model that has already been used to
    score cannot be un-scored.
    """
    path = model_path(model_version)
    if not path.exists():
        raise ArtifactMismatch(f"{path} does not exist; the registry expects it")

    actual = artifact_sha256(path)
    if actual != expected_sha256:
        raise ArtifactMismatch(
            f"{path} has digest {actual[:12]}… but the registry records "
            f"{expected_sha256[:12]}…. Refusing to score: a mismatched artifact "
            f"does not fail, it scores differently."
        )
    return path


CHAMPION_SQL = """
-- training_start and training_end travel with the champion because the scorer
-- refuses to score inside them: the register measures out-of-sample behaviour
-- or it measures nothing.
SELECT model_version, artifact_sha256, feature_names, offline_metrics, trained_at,
       training_start, training_end
  FROM register.model_registry
 ORDER BY trained_at DESC, model_version DESC
 LIMIT 1
"""


def champion(conn: Any) -> dict[str, Any] | None:
    """The model currently serving.

    In M3 that is simply the most recently registered, which is a placeholder
    and is named as one in sql/013. M5 replaces it with the promotion rule
    fixed in PREREGISTRATION.md, decided by evidence rather than recency.
    """
    with conn.cursor() as cur:
        cur.execute(CHAMPION_SQL)
        return cur.fetchone()


INSERT_MODEL_SQL = """
INSERT INTO register.model_registry
    (model_version, training_start, training_end, n_train_events, n_train_positives,
     feature_names, hyperparameters, offline_metrics, artifact_path, artifact_sha256,
     code_commit, registered_by_run)
VALUES (%(model_version)s, %(training_start)s, %(training_end)s, %(n_train_events)s,
        %(n_train_positives)s, %(feature_names)s, %(hyperparameters)s::jsonb,
        %(offline_metrics)s::jsonb, %(artifact_path)s, %(artifact_sha256)s,
        %(code_commit)s, %(run_id)s)
"""
