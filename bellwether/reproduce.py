"""Re-derive stored predictions and check they still hold (M3-FR-17, M3-FR-18).

FR-17 says a stored prediction shall be recomputable: same event, same state,
same model version, same `feature_hash`, same score. FR-18 is the one that makes
it mean something — a reproducibility claim nobody re-checks is a comment.

**How the state is rebuilt.** One pass, in `event_ts` order, folding as it goes,
using the same `observe` / `history_for` the scorer and the training matrix use.
When the pass reaches a sampled event it emits features from the state as it
stands *before* that event, which is the definition training uses.

**Two definitions, deliberately.** The scorer does not read state as of the
edit; it reads persisted state as of the moment it happens to run, which is
later — currently much later, while the backlog drains. Any revert discovered in
that gap is in the scorer's view and not in training's. So each sample is hashed
twice: once under the training-time definition, and once with the reverts
discovered between the edit and its scoring folded in.

That second hash is not a fallback to make the number look better. It is the
diagnosis. A mismatch that resolves under it says precisely which definition of
state the scorer was using, which is the difference between "we cannot reproduce
our own predictions" and a measured statement about train/serve skew.

**Only the hash is stored, not the vector.** So a mismatch under both definitions
says something differs without saying what. That is a real limit of what 120
bytes a row can prove, and it is published rather than glossed.
"""

from __future__ import annotations

import argparse
import pickle
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from bellwether import features, frame, knowability, registry, state
from bellwether.config import get_settings
from bellwether.db import advisory_lock, connect
from bellwether.runlog import RunContext, new_run_id

JOB = "reproduce"
REPRODUCE_LOCK_KEY = 815_009

_SAMPLE_SALT = "bellwether/reproduce/v1"
SAMPLE_PERCENT = 5

# The columns features.build reads, plus what the fold needs. Deliberately the
# same shape score.py selects: a reproduction that reads different columns is
# not a reproduction.
REPLAY_SQL = f"""
SELECT e.revid, e.old_revid, e.event_ts, e.ns, e.title, e.user_name, e.user_id,
       e.is_anon, e.is_temp, e.is_minor, e.is_bot, e.comment, e.comment_hidden,
       e.oldlen, e.newlen, e.tags, e.sampling_stratum,
       (e.tags && ARRAY['mw-undo','mw-rollback','mw-manual-revert']) AS is_reverting,
       {state.KNOWN_AT_SQL} AS reverted_known_at,
       p.feature_hash, p.score, p.model_version, p.scored_at
  FROM landing.rc_events e
  LEFT JOIN register.predictions p
         ON p.revid = e.revid AND p.role = 'champion'
 WHERE e.event_ts >= %(window_start)s
   AND e.event_ts <  %(window_end)s
 ORDER BY e.event_ts, e.revid
"""

# Reverts of this editor's or this page's earlier edits that became known
# AFTER the edit was made but BEFORE it was scored. Exactly the rows that
# separate the two definitions of state.
LATE_KNOWLEDGE_SQL = f"""
SELECT count(*) FILTER (WHERE e.user_name = %(user_name)s) AS editor_extra,
       count(*) FILTER (WHERE e.title     = %(title)s)     AS page_extra
  FROM landing.rc_events e
 WHERE (e.user_name = %(user_name)s OR e.title = %(title)s)
   AND e.event_ts < %(event_ts)s
   AND {state.KNOWN_AT_SQL} >  %(event_ts)s
   AND {state.KNOWN_AT_SQL} <= %(scored_at)s
"""

MODEL_SQL = """
SELECT model_version, artifact_sha256 FROM register.model_registry
 WHERE model_version = ANY(%(versions)s)
"""

INSERT_REPRODUCTION_SQL = """
INSERT INTO register.reproductions
    (window_start, window_end, sampled, hash_matched, score_matched,
     matched_at_scoring_time, unreproducible, model_versions, code_commit, run_id)
VALUES (%(window_start)s, %(window_end)s, %(sampled)s, %(hash_matched)s,
        %(score_matched)s, %(matched_at_scoring_time)s, %(unreproducible)s,
        %(model_versions)s, %(commit)s, %(run_id)s)
"""


class ReproductionFailure(RuntimeError):
    """A stored prediction could not be re-derived. M3-FR-17 is not met."""


def sampled(revid: int, percent: int = SAMPLE_PERCENT) -> bool:
    """Deterministic, so two runs over the same window check the same rows.

    Sampling at random would make a falling agreement rate indistinguishable
    from a different draw.
    """
    return frame.bucket(revid, _SAMPLE_SALT) < percent


def _load_models(conn: Any, versions: set[str]) -> dict[str, Any]:
    """Digest-verified before loading, exactly as the scorer does. A model that
    does not match its registry entry does not fail to load — it reproduces
    differently, which would read here as a reproducibility failure and is not
    one."""
    if not versions:
        return {}
    models = {}
    with conn.cursor() as cur:
        cur.execute(MODEL_SQL, {"versions": sorted(versions)})
        for row in cur.fetchall():
            path = registry.verify(row["model_version"], row["artifact_sha256"])
            with path.open("rb") as fh:
                models[row["model_version"]] = pickle.load(fh)  # noqa: S301 - hash-verified
    return models


def run(*, days: int = 2, percent: int = SAMPLE_PERCENT) -> dict[str, Any]:
    run_id = new_run_id()
    settings = get_settings()
    knowability.run_all()

    window_end = datetime.now(UTC)
    window_start = window_end - timedelta(days=days)

    with connect() as lock_conn, advisory_lock(lock_conn, REPRODUCE_LOCK_KEY) as acquired:
        if not acquired:
            print("reproduce: another run holds the lock, exiting cleanly")
            return {"skipped": True}

        with RunContext(run_id, job=JOB, window_from=window_start, window_to=window_end) as ctx:
            with connect() as conn, conn.cursor() as cur:
                cur.execute(REPLAY_SQL, {"window_start": window_start, "window_end": window_end})
                events = cur.fetchall()

            names = features.feature_names()
            st: dict[str, Any] = {}
            pending: list[tuple[datetime, dict[str, Any]]] = []
            checks: list[dict[str, Any]] = []

            for event in events:
                now = event["event_ts"]
                still = []
                for known_at, reverted in pending:
                    if known_at <= now:
                        state.observe_revert(st, reverted)
                    else:
                        still.append((known_at, reverted))
                pending = still

                if event["feature_hash"] is not None and sampled(event["revid"], percent):
                    history = state.history_for(st, event)
                    vector = features.build(event, history)
                    checks.append({"event": event, "history": history, "vector": vector})

                state.observe(st, event)
                if event["reverted_known_at"] is not None:
                    pending.append((event["reverted_known_at"], event))

            versions = {c["event"]["model_version"] for c in checks}
            with connect() as conn:
                models = _load_models(conn, versions)

                hash_matched = score_matched = late_matched = unreproducible = 0
                for check in checks:
                    event, vector = check["event"], check["vector"]
                    if features.feature_hash(vector) == event["feature_hash"]:
                        hash_matched += 1
                        matched_vector: dict[str, Any] | None = vector
                    else:
                        matched_vector = _try_at_scoring_time(conn, check, names)
                        if matched_vector is not None:
                            late_matched += 1
                        else:
                            unreproducible += 1

                    model = models.get(event["model_version"])
                    if matched_vector is not None and model is not None:
                        again = float(
                            model.predict_proba([[matched_vector[n] for n in names]])[0][1]
                        )
                        if abs(again - float(event["score"])) < 1e-9:
                            score_matched += 1

                with conn.cursor() as cur:
                    cur.execute(
                        INSERT_REPRODUCTION_SQL,
                        {
                            "window_start": window_start,
                            "window_end": window_end,
                            "sampled": len(checks),
                            "hash_matched": hash_matched,
                            "score_matched": score_matched,
                            "matched_at_scoring_time": late_matched,
                            "unreproducible": unreproducible,
                            "model_versions": sorted(versions),
                            "commit": settings.build_id,
                            "run_id": run_id,
                        },
                    )

            ctx.rows_read = len(events)
            ctx.rows_written = 1
            ctx.partial = unreproducible > 0

    total = len(checks)
    rate = hash_matched / total if total else 1.0
    print(f"reproduce: {total:,} of {len(events):,} predictions re-derived over {days}d")
    print(f"  feature hash matched          {hash_matched:>7,}  ({rate:.2%})")
    print(f"  matched only at scoring time  {late_matched:>7,}")
    print(f"  not reproducible either way   {unreproducible:>7,}")
    print(f"  score reproduced exactly      {score_matched:>7,}")

    if unreproducible:
        raise ReproductionFailure(
            f"{unreproducible:,} of {total:,} sampled predictions could not be re-derived "
            f"under either definition of state. Only the feature hash is stored, not the "
            f"vector, so this says something differs without saying what — start from the "
            f"features that read state."
        )

    return {
        "sampled": total,
        "hash_matched": hash_matched,
        "matched_at_scoring_time": late_matched,
        "unreproducible": unreproducible,
        "score_matched": score_matched,
        "agreement": round(rate, 6),
    }


def _try_at_scoring_time(
    conn: Any, check: dict[str, Any], names: list[str]
) -> dict[str, Any] | None:
    """Rebuild the vector with what the SCORER knew, not what training would.

    The scorer reads persisted state as of the moment it runs, so any revert
    discovered between the edit and its scoring is in its view and not in the
    training-time one. Folding those two counters in is the whole difference.
    """
    event, history = check["event"], check["history"]
    if event["scored_at"] is None:
        return None

    with conn.cursor() as cur:
        cur.execute(
            LATE_KNOWLEDGE_SQL,
            {
                "user_name": event["user_name"],
                "title": event["title"],
                "event_ts": event["event_ts"],
                "scored_at": event["scored_at"],
            },
        )
        extra = cur.fetchone() or {}

    adjusted = dict(history)
    adjusted["editor_edits_reverted"] += int(extra.get("editor_extra") or 0)
    adjusted["page_edits_reverted"] += int(extra.get("page_extra") or 0)
    vector = features.build(event, adjusted)
    return vector if features.feature_hash(vector) == event["feature_hash"] else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--percent", type=int, default=SAMPLE_PERCENT)
    args = parser.parse_args()
    run(days=args.days, percent=args.percent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
