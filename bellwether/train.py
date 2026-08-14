"""Train the champion and register it (M3-FR-14, M3-FR-15).

One model, trained once on a stated window, written to `models/` with its
metric card, and recorded in `register.model_registry` with the artifact's
SHA-256.

**No tuning.** M2 §6 forbade it there because KC-2 asked whether signal exists;
it stays forbidden here for a different reason. A model chosen by trying
several and keeping the best has been selected on the evaluation it is about to
be judged by, and every accuracy figure it produces afterwards is optimistic by
an amount nobody can measure. Hyperparameters are fixed in this file and change
only through the pre-registered rule in M5.

The version string is derived from the training window and the code commit, so
two models trained on the same data by the same code collide by name rather
than quietly becoming two entries claiming different things.
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from datetime import datetime
from typing import Any

from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, brier_score_loss

from bellwether import evaluate, knowability, promote, registry
from bellwether.config import get_settings
from bellwether.db import connect
from bellwether.runlog import RunContext, new_run_id

JOB = "train"

# Fixed here, not searched. See the module docstring.
HYPERPARAMETERS: dict[str, Any] = {
    "max_iter": 200,
    "learning_rate": 0.1,
    "random_state": evaluate.SEED,
}


def version_for(start: datetime, end: datetime, commit: str) -> str:
    return f"champion-{start:%Y%m%d}-{end:%Y%m%d}-{commit[:7]}"


def run(*, window_start: str, window_end: str) -> dict[str, Any]:
    settings = get_settings()
    run_id = new_run_id()
    start, end = evaluate.parse_when(window_start), evaluate.parse_when(window_end)

    # Before the data is touched, let alone fitted to.
    knowability.run_all()

    with connect() as conn:
        rows = evaluate.load(conn, window_start=start, window_end=end)

    if len(rows) < 500:
        print(f"train: only {len(rows)} matured events. Refusing to train on that.")
        return {"trained": False, "n": len(rows)}

    matrix, labels, names = evaluate.build_matrix(rows)
    version = version_for(start, end, settings.build_id)

    # M5-FR-6. The quartile boundaries P-5's segments are cut on, frozen here
    # with everything else this model is judged against.
    #
    # Recomputed per evaluation window they would move under the metric, and a
    # segment could regress because the bands shifted rather than because the
    # model did — blocking a promotion, or waving one through, for a reason that
    # has nothing to do with either model.
    segment_bands = {
        "edit_size": promote.bands_for(
            [abs((r["newlen"] or 0) - (r["oldlen"] or 0)) for r in rows]
        ),
        "page_activity": promote.bands_for([float(r.get("page_activity") or 0) for r in rows]),
    }

    model = HistGradientBoostingClassifier(**HYPERPARAMETERS)
    model.fit(matrix, labels)

    # In-sample, and labelled as such. The honest out-of-sample numbers come
    # from the rolling-origin evaluation and from the register once this model
    # has scored things it was never fitted to. Recorded because a metric card
    # without training metrics hides how much the model memorised.
    in_sample = model.predict_proba(matrix)[:, 1]
    metrics = {
        "in_sample_pr_auc": round(float(average_precision_score(labels, in_sample)), 6),
        "in_sample_brier": round(float(brier_score_loss(labels, in_sample)), 6),
        "base_rate": round(float(labels.mean()), 6),
        "note": "in-sample only; out-of-sample lives in outcome.evaluations and the register",
    }

    registry.MODELS_DIR.mkdir(exist_ok=True)
    artifact = registry.model_path(version)
    with artifact.open("wb") as fh:
        pickle.dump(model, fh, protocol=5)
    digest = registry.artifact_sha256(artifact)

    card = {
        "model_version": version,
        "training_window": [start, end],
        "n_train_events": len(labels),
        "n_train_positives": int(labels.sum()),
        "feature_names": names,
        "hyperparameters": HYPERPARAMETERS,
        "offline_metrics": metrics,
        "artifact_sha256": digest,
        "code_commit": settings.build_id,
        "sklearn_version": __import__("sklearn").__version__,
        "segment_bands": segment_bands,
    }
    card_file = registry.write_card(version, card)

    with RunContext(run_id, job=JOB, window_from=start, window_to=end) as ctx, connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                registry.INSERT_MODEL_SQL,
                {
                    "model_version": version,
                    "training_start": start,
                    "training_end": end,
                    "n_train_events": len(labels),
                    "n_train_positives": int(labels.sum()),
                    "feature_names": names,
                    "hyperparameters": json.dumps(HYPERPARAMETERS),
                    "offline_metrics": json.dumps(metrics),
                    "artifact_path": str(artifact.relative_to(registry.MODELS_DIR.parent)),
                    "artifact_sha256": digest,
                    "segment_bands": json.dumps(segment_bands),
                    "code_commit": settings.build_id,
                    "run_id": run_id,
                },
            )
        ctx.rows_read = len(labels)
        ctx.rows_written = 1

    print(f"train: {version}")
    print(f"  events {len(labels):,}  positives {int(labels.sum()):,}  features {len(names)}")
    print(
        f"  in-sample PR-AUC {metrics['in_sample_pr_auc']:.4f} "
        f"(base rate {metrics['base_rate']:.4f})"
    )
    print(f"  artifact {artifact.name}  sha256 {digest[:12]}...")
    print(f"  card     {card_file.name}")
    print("  commit models/ so the model is verifiable from git alone")

    return {"trained": True, "version": version, "sha256": digest, "metrics": metrics}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", required=True)
    args = parser.parse_args()
    run(window_start=args.start, window_end=args.end)
    return 0


if __name__ == "__main__":
    sys.exit(main())
