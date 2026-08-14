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


# The columns every caller needs, in one place, so the two resolvers below
# cannot answer with different shapes.
#
# training_start and training_end travel with the champion because the scorer
# refuses to score inside them: the register measures out-of-sample behaviour
# or it measures nothing.
_MODEL_COLUMNS = """
       m.model_version, m.artifact_sha256, m.feature_names, m.offline_metrics,
       m.trained_at, m.training_start, m.training_end
"""

# M5-FR-24. What the decision log promoted, which is not the same question as
# what was trained most recently.
PROMOTED_CHAMPION_SQL = f"""
SELECT {_MODEL_COLUMNS}
  FROM decide.champion_history h
  JOIN register.model_registry m ON m.model_version = h.model_version
 ORDER BY h.effective_from DESC, h.history_id DESC
 LIMIT 1
"""

# Before the first promotion there is nothing to promote from, so recency is
# the answer. Named as a fallback rather than left as the rule, because "most
# recently registered" IS the wrong rule once decisions exist — it would hand
# production to a challenger the moment it was trained.
NEWEST_MODEL_SQL = f"""
SELECT {_MODEL_COLUMNS}
  FROM register.model_registry m
 ORDER BY m.trained_at DESC, m.model_version DESC
 LIMIT 1
"""

# Retained under its old name: sql/013 and the M3 summary both refer to it.
CHAMPION_SQL = NEWEST_MODEL_SQL


def champion(conn: Any) -> dict[str, Any] | None:
    """The model currently serving — the one the decision log promoted.

    M3 answered this with "most recently registered", named as a placeholder in
    sql/013 when it was written. That rule is actively wrong once M5 runs: a
    challenger is registered the moment it is trained, and recency would hand it
    production without it having proved anything, which is the exact failure the
    pre-registered promotion rule exists to prevent.

    The fallback to recency survives only for the state before any promotion has
    happened, where there is genuinely nothing else to answer with.
    """
    with conn.cursor() as cur:
        cur.execute(PROMOTED_CHAMPION_SQL)
        promoted = cur.fetchone()
        if promoted:
            return promoted
        cur.execute(NEWEST_MODEL_SQL)
        return cur.fetchone()


ROLLED_BACK_SQL = """
SELECT 1 FROM decide.model_decisions
 WHERE decision = 'rollback' AND challenger_version = %(version)s
 LIMIT 1
"""


def challenger(conn: Any, champion_version: str | None) -> dict[str, Any] | None:
    """The model in shadow, or None.

    Derived rather than recorded: it is the most recently trained model, unless
    that model is already the champion. No table is needed for this, and more
    importantly none is needed for P-4 either — shadow begins when the model was
    trained, so `trained_at` IS the clock, and M5-FR-19's rule that a new
    retrain resets P-3 and P-4 falls out rather than being enforced.

    A model that has been rolled back is never returned. M5-FR-27: re-promoting
    it would take the identical shadow record that promoted it the first time
    and read it a second time, which is not new evidence. It needs a fresh
    training run to become a candidate again.
    """
    with conn.cursor() as cur:
        cur.execute(NEWEST_MODEL_SQL)
        newest = cur.fetchone()
        if not newest or newest["model_version"] == champion_version:
            return None
        cur.execute(ROLLED_BACK_SQL, {"version": newest["model_version"]})
        if cur.fetchone():
            return None
    return newest


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
