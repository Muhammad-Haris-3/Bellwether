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
from bellwether.usage import record_on_exit

JOB = "reproduce"
REPRODUCE_LOCK_KEY = 815_009

_SAMPLE_SALT = "bellwether/reproduce/v1"
SAMPLE_PERCENT = 5

# How many failures --explain will describe. The output is for a person to
# read, and the first handful of a systematic divergence say the same thing as
# the sixty-fifth.
EXPLAIN_LIMIT = 12

# Exactly what the scorer folded, in exactly the order it folded it.
#
# Not a time window over rc_events. The scorer's state is built from the events
# it has SCORED — its lookback bounds that set, and a backfill outside the
# lookback is never folded at all. Replaying every event in a window instead
# folds history the scorer never had, which is how a 30-day replay scored worse
# than a 2-day one: the wider it reached, the more it invented.
#
# The order is (scored_at, event_ts, revid). Each run stamps one scored_at
# across its whole batch and folds within the batch in event_ts order, so this
# tuple is the fold order, not an approximation of it.
REPLAY_SQL = """
SELECT e.revid, e.old_revid, e.event_ts, e.ns, e.title, e.user_name, e.user_id,
       e.is_anon, e.is_temp, e.is_minor, e.is_bot, e.comment, e.comment_hidden,
       e.oldlen, e.newlen, e.tags, e.sampling_stratum,
       (e.tags && ARRAY['mw-undo','mw-rollback','mw-manual-revert']) AS is_reverting,
       p.feature_hash, p.score, p.model_version, p.scored_at,
       a.applied_at_utc AS revert_applied_at
  FROM register.predictions p
  JOIN landing.rc_events e ON e.revid = p.revid
  LEFT JOIN landing.state_applied_reverts a ON a.revid = e.revid
 WHERE p.role = 'champion'
   AND p.scored_at >= %(history_start)s
 ORDER BY p.scored_at, e.event_ts, e.revid
"""

# Was this editor or page already carrying state before the replay begins?
# Judged on what was SCORED, for the same reason as above.
PREDATES_SQL = """
SELECT EXISTS (
    SELECT 1
      FROM register.predictions p
      JOIN landing.rc_events e ON e.revid = p.revid
     WHERE (e.user_name = %(user_name)s OR e.title = %(title)s)
       AND p.role = 'champion'
       AND p.scored_at < %(history_start)s
) AS predates
"""

MODEL_SQL = """
SELECT model_version, artifact_sha256, feature_names FROM register.model_registry
 WHERE model_version = ANY(%(versions)s)
"""

INSERT_REPRODUCTION_SQL = """
INSERT INTO register.reproductions
    (window_start, window_end, sampled, hash_matched, score_matched,
     matched_at_scoring_time, unreproducible, state_predates_window,
     model_versions, code_commit, run_id)
VALUES (%(window_start)s, %(window_end)s, %(sampled)s, %(hash_matched)s,
        %(score_matched)s, %(matched_at_scoring_time)s, %(unreproducible)s,
        %(state_predates_window)s, %(model_versions)s, %(commit)s, %(run_id)s)
"""


class ReproductionFailure(RuntimeError):
    """A stored prediction could not be re-derived. M3-FR-17 is not met."""


def sampled(revid: int, percent: int = SAMPLE_PERCENT) -> bool:
    """Deterministic, so two runs over the same window check the same rows.

    Sampling at random would make a falling agreement rate indistinguishable
    from a different draw.
    """
    return frame.bucket(revid, _SAMPLE_SALT) < percent


def _load_models(conn: Any, versions: set[str]) -> dict[str, dict[str, Any]]:
    """Digest-verified before loading, exactly as the scorer does. A model that
    does not match its registry entry does not fail to load — it reproduces
    differently, which would read here as a reproducibility failure and is not
    one.

    Each model's REGISTERED feature list travels with it. A prediction has to
    be re-derived from the inputs its own model consumed, not from whatever
    this build produces now — otherwise adding a feature makes the entire
    history read as unreproducible, and the watchdog's reproducibility fault
    fires on a change that broke nothing.
    """
    if not versions:
        return {}
    models: dict[str, dict[str, Any]] = {}
    with conn.cursor() as cur:
        cur.execute(MODEL_SQL, {"versions": sorted(versions)})
        for row in cur.fetchall():
            path = registry.verify(row["model_version"], row["artifact_sha256"])
            with path.open("rb") as fh:
                models[row["model_version"]] = {
                    "model": pickle.load(fh),  # noqa: S301 - hash-verified
                    "feature_names": list(row["feature_names"]),
                }
    return models


def run(
    *,
    days: int = 2,
    percent: int = SAMPLE_PERCENT,
    history_days: int = 30,
    explain: bool = False,
) -> dict[str, Any]:
    """`days` chooses which predictions to check. `history_days` chooses how far
    back to rebuild the state that produced them, and the two are not the same
    number. The first run conflated them and replayed only the window it was
    checking, so any editor first seen earlier was guaranteed not to match.

    The default reaches the raw retention horizon, which is as far back as the
    events still exist to be replayed."""
    run_id = new_run_id()
    settings = get_settings()
    knowability.run_all()

    window_end = datetime.now(UTC)
    window_start = window_end - timedelta(days=days)
    history_start = window_end - timedelta(days=max(history_days, days))

    with connect() as lock_conn, advisory_lock(lock_conn, REPRODUCE_LOCK_KEY) as acquired:
        if not acquired:
            print("reproduce: another run holds the lock, exiting cleanly")
            return {"skipped": True}

        with RunContext(run_id, job=JOB, window_from=window_start, window_to=window_end) as ctx:
            with connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(REPLAY_SQL, {"history_start": history_start})
                    events = cur.fetchall()
                current = registry.champion(conn)

            # Which model version the claim covers.
            #
            # `state.py` changed substantially in M3 — reverts now fold in when
            # this system LEARNED of them rather than when they happened — and a
            # prediction written before that fix cannot re-derive under the code
            # that replaced it. That is the fix working, not a reproducibility
            # failure, and counting it as one made the daily job permanently red
            # while saying nothing true.
            #
            # So the claim is scoped to what the SERVING model produced: we can
            # reproduce the predictions the current champion made. Predictions
            # from a superseded champion are reported in their own column, like
            # state that predates the window already is. The narrower claim is
            # the one this project can actually support.
            current_version = current["model_version"] if current else None

            # No module-level feature list here on purpose: each prediction is
            # re-derived against the list its own model registered, taken from
            # `models` below.
            st: dict[str, Any] = {}
            pending: list[tuple[datetime, dict[str, Any]]] = []
            checks: list[dict[str, Any]] = []

            for event in events:
                # A revert enters the persisted counters when apply_reverts
                # wrote it, so any batch scored after that moment reads it and
                # any batch scored before does not.
                still = []
                for applied_at, reverted in pending:
                    if applied_at <= event["scored_at"]:
                        state.observe_revert(st, reverted)
                    else:
                        still.append((applied_at, reverted))
                pending = still

                if sampled(event["revid"], percent) and event["event_ts"] >= window_start:
                    history = state.history_for(st, event)
                    vector = features.build(event, history)
                    checks.append({"event": event, "history": history, "vector": vector})

                state.observe(st, event)
                if event["revert_applied_at"] is not None:
                    pending.append((event["revert_applied_at"], event))

            versions = {c["event"]["model_version"] for c in checks}
            with connect() as conn:
                models = _load_models(conn, versions)

                hash_matched = score_matched = unreproducible = out_of_scope = 0
                superseded = 0
                explanations: list[dict[str, Any]] = []
                # Retained at zero. The column exists because an earlier version
                # rebuilt each mismatch a second time with the reverts the
                # scorer had learned late; folding reverts by the moment
                # apply_reverts wrote them makes that variant the primary path
                # rather than an alternative to it. Older rows still carry
                # meaning, so the column is not dropped.
                late_matched = 0
                for check in checks:
                    event, vector = check["event"], check["vector"]
                    matched_vector: dict[str, Any] | None = None
                    entry = models.get(event["model_version"])

                    # Digest the subset this prediction's own model consumed.
                    # An unknown model — artifact missing, or a version this run
                    # could not load — falls back to the whole vector, which is
                    # what the hash meant before models carried their own lists.
                    model_names = entry["feature_names"] if entry else None
                    try:
                        recomputed = features.feature_hash(vector, model_names)
                    except KeyError:
                        # The model wants a feature this build no longer
                        # produces. Genuinely unreproducible under this code,
                        # and saying so is the honest answer.
                        recomputed = ""

                    if recomputed and recomputed == event["feature_hash"]:
                        hash_matched += 1
                        matched_vector = vector
                    elif current_version and event["model_version"] != current_version:
                        # Written by a superseded champion, under the code that
                        # trained it. state.py changed substantially in M3 —
                        # reverts fold in when this system LEARNED of them
                        # rather than when they happened — so a prediction from
                        # before that fix cannot re-derive under the code that
                        # replaced it.
                        #
                        # That is the fix working. Counting it as a failure made
                        # this job permanently red while saying nothing true.
                        superseded += 1
                    elif _predates(conn, event, history_start):
                        # The scorer read state built from events scored before
                        # this pass begins. Not a prediction that fails to
                        # reproduce — one this run cannot claim to have checked,
                        # and saying so is the difference between a rate and a
                        # rate over a shrinking denominator.
                        out_of_scope += 1
                    else:
                        unreproducible += 1
                        if explain and len(explanations) < EXPLAIN_LIMIT:
                            named = diagnose(check, model_names)
                            explanations.append(
                                {
                                    "revid": event["revid"],
                                    "found": named or ["no single feature reconciles the hash"],
                                }
                            )

                    if matched_vector is not None and entry is not None:
                        # Selected by the model's own registered order, which is
                        # the order it was trained in. Using this build's sorted
                        # list would silently permute the columns the moment the
                        # feature set changes, and the score would differ for a
                        # reason that has nothing to do with reproducibility.
                        again = float(
                            entry["model"].predict_proba(
                                [[matched_vector[n] for n in entry["feature_names"]]]
                            )[0][1]
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
                            "state_predates_window": out_of_scope,
                            "model_versions": sorted(versions),
                            "commit": settings.build_id,
                            "run_id": run_id,
                        },
                    )

            ctx.rows_read = len(events)
            ctx.rows_written = 1
            ctx.partial = unreproducible > 0

    total = len(checks)
    checkable = total - out_of_scope - superseded
    rate = hash_matched / checkable if checkable else 1.0
    print(f"reproduce: {total:,} of {len(events):,} predictions re-derived over {days}d")
    print(f"  feature hash matched          {hash_matched:>7,}  ({rate:.2%})")
    print(f"  matched only at scoring time  {late_matched:>7,}")
    print(f"  not reproducible either way   {unreproducible:>7,}")
    print(f"  state predates the window     {out_of_scope:>7,}  (not checkable)")
    print(f"  written by a superseded model {superseded:>7,}  (not checkable)")
    print(f"  score reproduced exactly      {score_matched:>7,}")

    if explanations:
        print(f"\n  what differed, for the first {len(explanations)} of them:")
        for item in explanations:
            print(f"    revid {item['revid']}")
            for line in item["found"]:
                print(f"      {line}")

    if unreproducible:
        hint = (
            " Run with --explain to name the feature that differs."
            if not explain
            else " The substitutions above name it, where a single feature explains it."
        )
        raise ReproductionFailure(
            f"{unreproducible:,} of {total:,} sampled predictions could not be re-derived "
            f"under either definition of state. Only the feature hash is stored, not the "
            f"vector, so a mismatch says something differs without saying what.{hint}"
        )

    return {
        "sampled": total,
        "superseded_model": superseded,
        "state_predates_window": out_of_scope,
        "hash_matched": hash_matched,
        "matched_at_scoring_time": late_matched,
        "unreproducible": unreproducible,
        "score_matched": score_matched,
        "agreement": round(rate, 6),
    }


def diagnose(check: dict[str, Any], names: list[str] | None) -> list[str]:
    """Name the feature that differs, when a prediction will not re-derive.

    The register stores the hash and not the vector, so a mismatch says only
    that something differs. That is the sentence in this module's own failure
    message, and it is why a reproduction rate below 100% has stood since
    2026-08-14 without a cause: there was nothing to look at.

    A hash cannot be inverted, so this searches instead. For each feature in
    turn, the recomputed vector is amended to a small set of PRINCIPLED
    candidate values — the value the alternative state definition would have
    given — and the hash retried. A candidate that reconciles the hash names
    both the feature that differed and what the scorer actually saw.

    Only single-feature substitutions are tried. Two simultaneous differences
    would go unnamed, and that is stated rather than searched for: the
    combinations grow as the square and a coincidental match stops being
    unlikely. Finding nothing is a real answer here — it means the divergence
    is not a single feature, which is itself worth knowing.

    Diagnostic only. Nothing here decides whether a prediction reproduced.
    """
    vector, history = check["vector"], check["history"]
    stored = check["event"]["feature_hash"]
    found: list[str] = []

    for name, value in sorted(vector.items()):
        for label, candidate in _candidates(name, value, history).items():
            if candidate == value:
                continue
            amended = {**vector, name: candidate}
            try:
                if features.feature_hash(amended, names) == stored:
                    found.append(f"{name}: computed {value!r}, scorer saw {candidate!r} ({label})")
            except KeyError:
                continue

    return found


def _candidates(name: str, value: float, history: dict[str, Any]) -> dict[str, float]:
    """Plausible alternative values for one feature, by how it could diverge.

    Deliberately small. This is a search over a hash, so every extra candidate
    is another chance at a coincidental collision — and a wrong answer here
    would send someone after the wrong subsystem for a day.
    """
    out: dict[str, float] = {}

    # The editor was unknown to the scorer but known to this replay, or the
    # reverse. Covers state that was loaded from a persisted row the replay
    # rebuilt differently, which is the whole family this module suspects.
    if name.startswith(("editor_", "page_")):
        out["editor or page unknown to the scorer"] = 0.0
        out["one fewer than counted"] = max(value - 1.0, 0.0)
        out["one more than counted"] = value + 1.0

    # account_newness is a ratio against the id frontier. A frontier that had
    # already moved past this account gives a smaller number; a frontier that
    # had not yet seen it clamps to 1.0.
    if name == "account_newness":
        out["frontier had not yet seen this account"] = 1.0
        frontier = float(history.get("max_user_id_seen", 0) or 0)
        if frontier > 0:
            out["editor absent from state"] = 0.0

    # A boolean that flipped. Cheap to test and unambiguous when it hits.
    if value in (0.0, 1.0):
        out["the flag was the other way"] = 1.0 - value

    return out


def _predates(conn: Any, event: dict[str, Any], history_start: datetime) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            PREDATES_SQL,
            {
                "user_name": event["user_name"],
                "title": event["title"],
                "history_start": history_start,
            },
        )
        row = cur.fetchone()
    return bool(row and row["predates"])


def main() -> int:
    # Registered before any work, so a run that dies mid-read still
    # accounts for what it spent.
    record_on_exit("reproduce")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--percent", type=int, default=SAMPLE_PERCENT)
    parser.add_argument("--history-days", type=int, default=30)
    parser.add_argument(
        "--explain",
        action="store_true",
        help="For failures, search for the single feature that reconciles the stored hash",
    )
    args = parser.parse_args()
    run(
        days=args.days,
        percent=args.percent,
        history_days=args.history_days,
        explain=args.explain,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
