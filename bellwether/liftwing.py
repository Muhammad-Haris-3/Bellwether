"""The institutional benchmark (M4-FR-18 to FR-21).

Wikimedia runs `revertrisk-language-agnostic` in production, against the same
edits this project scores. SRS §6.4 recorded — before any model existed — that
it is expected to win, and that recording it in advance is what makes the
eventual number worth reading.

**Not to win.** The deliverable was never "beat Wikimedia"; it is a system that
grades itself honestly and maintains itself unattended. A benchmark published
only when it flatters is not a benchmark.

**Sampled, never exhaustive.** One HTTP request per revision against a free,
donation-funded service, for a comparison that a few hundred paired events
settles as well as ten thousand would. The sample rate is published beside
every figure it produces (M4-FR-25).

**A gate is a gap, not a workaround.** DS-3's auth column reads `TBC` in the
SRS and Wikimedia has been moving inference endpoints behind access tokens. If
this returns 401 or 403 the attempt is recorded as `gated` and the comparison
stays absent. Substituting some other reachable model would answer a question
nobody asked, in a row that looks exactly like the one that was asked for.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import httpx

from bellwether.config import get_settings
from bellwether.db import advisory_lock, connect
from bellwether.http import DEFAULT_TIMEOUT, RateLimiter
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


class Gated(RuntimeError):
    """The service requires credentials this project does not have (M4-FR-21)."""


# Matured predictions with no Lift Wing score yet, newest first.
#
# Newest first, unlike everywhere else in this project: the benchmark exists to
# describe how the two models compare NOW, and spending a limited request
# budget on the oldest unscored events would answer that question last.
UNSCORED_SQL = """
SELECT p.revid
  FROM register.predictions p
  JOIN landing.rc_events e ON e.revid = p.revid
 WHERE p.role = 'champion'
   AND NOT EXISTS (SELECT 1 FROM outcome.liftwing_scores s WHERE s.revid = p.revid)
   AND EXTRACT(epoch FROM now() - p.event_ts) >= %(maturity)s
 ORDER BY p.event_ts DESC
 LIMIT %(limit)s
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


def score_one(client: httpx.Client, limiter: RateLimiter, revid: int) -> dict[str, Any] | None:
    """One revision. Returns None when the service has no answer for it.

    A revision Lift Wing declines to score — deleted, suppressed, or simply
    unknown to it — is not an error and not a zero. It is absent, and absent is
    what gets recorded, because a missing score imputed as 0.0 would make their
    model look wrong about an edit it never saw.
    """
    limiter.wait()
    response = client.post(ENDPOINT, json={"rev_id": revid, "lang": "en"})

    if response.status_code in (401, 403):
        raise Gated(f"{response.status_code} from Lift Wing: {response.text[:200]}")
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        response.raise_for_status()

    body = response.json()
    output = body.get("output") or {}
    probability = (output.get("probabilities") or {}).get("true")
    if probability is None:
        return None
    return {
        "score": float(probability),
        "model_version": str(body.get("model_version") or body.get("model_name") or ""),
    }


def run(*, limit: int = DEFAULT_BATCH, maturity_seconds: int = 48 * 3600) -> dict[str, Any]:
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
                cur.execute(UNSCORED_SQL, {"maturity": maturity_seconds, "limit": limit})
                revids = [r["revid"] for r in cur.fetchall()]

        if not revids:
            print("liftwing: nothing matured and unscored")
            return {"requested": 0, "fetched": 0, "status": "ok"}

        limiter = RateLimiter(REQUESTS_PER_MINUTE)
        client = httpx.Client(
            timeout=DEFAULT_TIMEOUT,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )

        with RunContext(run_id, job=JOB) as ctx, connect() as conn:
            try:
                for revid in revids:
                    result = score_one(client, limiter, revid)
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
                    fetched += 1
            except Gated as exc:
                # M4-FR-21. Recorded, not routed around.
                status, detail = "gated", str(exc)[:500]
            except httpx.HTTPError as exc:
                status, detail = "unavailable", f"{type(exc).__name__}: {exc}"[:500]
            finally:
                client.close()

            if status == "ok" and fetched < len(revids):
                status = "partial"
                detail = f"{len(revids) - fetched} revisions returned no score"

            with conn.cursor() as cur:
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
    parser.add_argument("--maturity-hours", type=int, default=48)
    args = parser.parse_args()
    run(limit=args.limit, maturity_seconds=args.maturity_hours * 3600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
