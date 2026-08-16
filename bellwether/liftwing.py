"""The institutional benchmark (M4-FR-18 to FR-21).

Wikimedia runs `revertrisk-language-agnostic` in production, against the same
edits this project scores. SRS §6.4 recorded — before any model existed — that
it is expected to win, and that recording it in advance is what makes the
eventual number worth reading.

**Not to win.** The deliverable was never "beat Wikimedia"; it is a system that
grades itself honestly and maintains itself unattended. A benchmark published
only when it flatters is not a benchmark.

**Sampled, never exhaustive** (M4-FR-25). A deterministic bucket on revid, at a
rate fixed here rather than implied by whatever batch size a workflow happened
to pass. One HTTP request per revision against a free, donation-funded service,
for a comparison that a few hundred paired events settles as well as ten
thousand would.

**Fetched before maturity, compared after.** The first version would only fetch
a revision once its own outcome had matured, which meant the first run had
nothing to do and returned without recording that it had tried — so the one
thing worth knowing early, whether this endpoint answers at all, stayed
unknown. Scores do not need a matured label to be *collected*; only the
comparison does. Fetching early also collects them while the revision still
exists to be scored.

**A gate is a gap, not a workaround.** DS-3's auth column reads `TBC` in the
SRS and Wikimedia has been moving inference endpoints behind access tokens. If
this returns 401 or 403 the attempt is recorded as `gated` and the comparison
stays absent. Substituting some other reachable model would answer a question
nobody asked, in a row that looks exactly like the one that was asked for.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from bellwether import frame
from bellwether.config import get_settings
from bellwether.db import advisory_lock, connect
from bellwether.http import DEFAULT_TIMEOUT, RateLimiter, UpstreamError
from bellwether.runlog import RunContext, new_run_id
from bellwether.schema import require_current

JOB = "liftwing"
LIFTWING_LOCK_KEY = 815_011

ENDPOINT = (
    "https://api.wikimedia.org/service/lw/inference/v1/models/revertrisk-language-agnostic:predict"
)
MODEL_NAME = "revertrisk-language-agnostic"

# Well under anything Wikimedia publishes for anonymous inference traffic. This
# job has no deadline — it fills a benchmark that is checked daily at most — so
# there is no reason to push it.
REQUESTS_PER_MINUTE = 30

# Per run. At 30/min a batch of 200 takes about seven minutes, which fits the
# workflow budget with room to spare.
DEFAULT_BATCH = 200

# The published sample rate (M4-FR-25). A fixed fraction of the register,
# chosen deterministically so the same revisions are always eligible and the
# sample cannot drift with batch size or scheduling.
SAMPLE_PERCENT = 10
_SAMPLE_SALT = "bellwether/liftwing/v1"

# A circuit breaker, so the two failure modes stay distinguishable.
#
# One revision failing after its own retries is not the service being down —
# skip it and keep the batch. This many in a row is, and continuing would spend
# the remaining budget hammering a server that is already struggling.
CONSECUTIVE_FAILURES_BEFORE_STOPPING = 5


class Gated(RuntimeError):
    """The service requires credentials this project does not have (M4-FR-21)."""


# Predictions with no Lift Wing score yet, newest first.
#
# Newest first, unlike everywhere else in this project: the benchmark describes
# how the two models compare NOW, and spending a limited request budget on the
# oldest events would answer that question last.
#
# No maturity filter. A score does not need a matured label to be collected —
# only the comparison does — and waiting meant the job could not even discover
# whether the service answers.
UNSCORED_SQL = """
SELECT p.revid
  FROM register.predictions p
 WHERE p.role = 'champion'
   AND NOT EXISTS (SELECT 1 FROM outcome.liftwing_scores s WHERE s.revid = p.revid)
 ORDER BY p.event_ts DESC
 LIMIT %(candidates)s
"""

INSERT_SCORE_SQL = """
INSERT INTO outcome.liftwing_scores (revid, score, model_name, model_version, fetched_by_run)
VALUES (%(revid)s, %(score)s, %(model_name)s, %(model_version)s, %(run_id)s)
ON CONFLICT (revid) DO NOTHING
"""

INSERT_ATTEMPT_SQL = """
INSERT INTO outcome.liftwing_attempts (requested, fetched, status, detail, run_id)
VALUES (%(requested)s, %(fetched)s, %(status)s, %(detail)s, %(run_id)s)
"""


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)
def score_one(client: httpx.Client, limiter: RateLimiter, revid: int) -> dict[str, Any] | None:
    """One revision. Returns None when the service has no answer for it.

    A revision Lift Wing declines to score — deleted, suppressed, or simply
    unknown to it — is not an error and not a zero. It is absent, and absent is
    what gets recorded, because a missing score imputed as 0.0 would make their
    model look wrong about an edit it never saw.

    Retried on 5xx and 429, like every other upstream call in this project. The
    first version was not, and a single 503 partway through ended the run with
    55 of 200 fetched — a transient blip on someone else's server reported as
    the service being unavailable.

    4xx other than 429 are not retried: a request that is malformed stays
    malformed however often it is sent.
    """
    limiter.wait()
    response = client.post(ENDPOINT, json={"rev_id": revid, "lang": "en"})

    if response.status_code in (401, 403):
        raise Gated(f"{response.status_code} from Lift Wing: {response.text[:200]}")
    if response.status_code in (404, 422):
        # 404 is a revision they have never heard of. 422 is one they have and
        # decline to score — `revision_info_deleted`, for a revision deleted or
        # suppressed between our ingesting it and our asking about it.
        #
        # Both are the absence this docstring describes, and they were treated
        # differently: 404 returned None while 422 fell through to the generic
        # UpstreamError below and ended the entire run. Fetching newest first
        # makes a recently deleted revision one of the likelier things in any
        # batch, so this was not a rare path — it killed the job twice in the
        # week it was noticed and left the benchmark reading as healthy.
        return None
    if response.status_code == 429:
        # Their instruction beats our backoff when they gave one.
        retry_after = response.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            time.sleep(min(int(retry_after), 120))
        response.raise_for_status()
    if response.status_code >= 500:
        response.raise_for_status()
    if response.status_code >= 400:
        raise UpstreamError(f"{response.status_code} from Lift Wing: {response.text[:200]}")

    body = response.json()
    output = body.get("output") or {}
    probability = (output.get("probabilities") or {}).get("true")
    if probability is None:
        return None
    return {
        "score": float(probability),
        "model_version": str(body.get("model_version") or body.get("model_name") or ""),
    }


def sampled(revid: int, percent: int = SAMPLE_PERCENT) -> bool:
    """Deterministic, so the eligible set does not shift with batch size."""
    return frame.bucket(revid, _SAMPLE_SALT) < percent


def run(*, limit: int = DEFAULT_BATCH, percent: int = SAMPLE_PERCENT) -> dict[str, Any]:
    run_id = new_run_id()
    settings = get_settings()
    fetched = 0
    status = "ok"
    detail: str | None = None

    with connect() as lock_conn, advisory_lock(lock_conn, LIFTWING_LOCK_KEY) as acquired:
        if not acquired:
            print("liftwing: another run holds the lock, exiting cleanly")
            return {"skipped": True}

        with connect() as conn:
            require_current(conn)
            with conn.cursor() as cur:
                # Over-fetch candidates, then keep the sampled ones. Sampling in
                # SQL would need the same blake2b bucket in Postgres, and two
                # implementations of a sampling rule is how a frame drifts.
                cur.execute(UNSCORED_SQL, {"candidates": limit * (100 // max(percent, 1))})
                revids = [r["revid"] for r in cur.fetchall() if sampled(r["revid"], percent)][
                    :limit
                ]

        if not revids:
            # Recorded, not returned silently. "There was nothing to do" and
            # "nobody ran this" are different facts, and the first run of this
            # job produced the second by accident.
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    INSERT_ATTEMPT_SQL,
                    {
                        "requested": 0,
                        "fetched": 0,
                        "status": "ok",
                        "detail": "no unscored predictions in the sample",
                        "run_id": run_id,
                    },
                )
            print("liftwing: nothing unscored in the sample")
            return {"requested": 0, "fetched": 0, "status": "ok"}

        limiter = RateLimiter(REQUESTS_PER_MINUTE)
        client = httpx.Client(
            timeout=DEFAULT_TIMEOUT,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )

        with RunContext(run_id, job=JOB) as ctx:
            unexpected: Exception | None = None
            try:
                # The connection lives inside the loop's scope, and every score
                # is committed as it lands.
                #
                # This loop spends minutes waiting on the rate limiter. Holding
                # one transaction open across all of it left the session idle IN
                # transaction — the exact shape `db.advisory_lock` documents,
                # with the exact same ending:
                #
                #   IdleInTransactionSessionTimeout: terminating connection due
                #   to idle-in-transaction timeout
                #
                # Committing each row keeps the session merely idle, which no
                # timeout collects, and means a batch that dies partway keeps
                # the scores it already spent someone else's bandwidth on.
                # ON CONFLICT DO NOTHING makes re-running it free.
                with connect() as conn:
                    consecutive = 0
                    for revid in revids:
                        try:
                            result = score_one(client, limiter, revid)
                        except (httpx.HTTPError, UpstreamError) as exc:
                            # One revision failing after its retries is not the
                            # service being down. Skip it and keep the rest of
                            # the batch, which the first version threw away.
                            #
                            # UpstreamError belongs here for the same reason:
                            # it is a RuntimeError, not an httpx one, so it used
                            # to sail past this handler and the run-level one
                            # below and take the whole job with it.
                            consecutive += 1
                            if consecutive >= CONSECUTIVE_FAILURES_BEFORE_STOPPING:
                                raise
                            detail = f"{type(exc).__name__} on {revid}"
                            continue
                        consecutive = 0
                        if result is None:
                            continue
                        with conn.cursor() as cur:
                            cur.execute(
                                INSERT_SCORE_SQL,
                                {
                                    "revid": revid,
                                    "score": result["score"],
                                    "model_name": MODEL_NAME,
                                    "model_version": result["model_version"],
                                    "run_id": run_id,
                                },
                            )
                        conn.commit()
                        fetched += 1
            except Gated as exc:
                # M4-FR-21. Recorded, not routed around.
                status, detail = "gated", str(exc)[:500]
            except (httpx.HTTPError, UpstreamError) as exc:
                status, detail = "unavailable", f"{type(exc).__name__}: {exc}"[:500]
            except Exception as exc:
                # Anything else at all, including the database going away
                # underneath the loop.
                #
                # Not caught to be survived — it is re-raised below, and the run
                # still goes red. Caught so that the attempt row gets written
                # first. Crashing before writing it is what left /metrics
                # serving a two-day-old `191 of 191 (ok)` next to a job that had
                # been dead since, and a benchmark reporting its own silence as
                # success is worse than one reporting nothing at all.
                #
                # `unavailable` is the nearest of the four statuses the column
                # allows; the detail says what actually happened.
                status = "unavailable"
                detail = f"{type(exc).__name__}: {exc}"[:500]
                unexpected = exc
            finally:
                client.close()

                if status == "ok" and fetched < len(revids):
                    status = "partial"
                    # Two causes, deliberately not merged: Lift Wing having no
                    # opinion about a revision, and a request that kept failing.
                    # The first is normal and the second is worth noticing.
                    detail = (
                        f"{len(revids) - fetched} of {len(revids)} produced no score "
                        f"(declined by the service, or failed after retries)"
                    )

                # A connection of its own, for two reasons. It records the
                # attempt even when the fetch connection is the thing that died.
                # And `attempted_at` defaults to now(), which in Postgres is
                # TRANSACTION start time — sharing the fetch's transaction
                # stamped every attempt with the moment the run began rather
                # than the moment it finished, understating each row by however
                # long the batch took.
                with connect() as attempt_conn, attempt_conn.cursor() as cur:
                    cur.execute(
                        INSERT_ATTEMPT_SQL,
                        {
                            "requested": len(revids),
                            "fetched": fetched,
                            "status": status,
                            "detail": detail,
                            "run_id": run_id,
                        },
                    )

            ctx.rows_read = len(revids)
            ctx.rows_written = fetched
            ctx.partial = status != "ok"

            if unexpected is not None:
                raise unexpected

    print(f"liftwing: {fetched:,} of {len(revids):,} scored — {status}")
    if detail:
        print(f"  {detail}")
    if status == "gated":
        print(
            "  Recorded as an unmet dependency. The comparison stays absent rather "
            "than being filled with a different model that happens to be reachable."
        )
    return {"requested": len(revids), "fetched": fetched, "status": status}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--percent", type=int, default=SAMPLE_PERCENT)
    args = parser.parse_args()
    run(limit=args.limit, percent=args.percent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
