"""The triage queue and what a reviewer does with it (M6 §5, §6).

**Almost every item here is immature, and that is what the queue is for.**
Maturity is seven days; a queue of recent edits is therefore almost entirely
items whose outcome nobody knows. That is correct — triage happens before the
answer exists — and it is also the easiest place in this project to mislead
somebody.

A reviewer who sees 0.92 beside an edit and no maturity marker reads it as
*this edit was bad*. It means *this model thinks this edit is likely to be
reverted, and nobody knows yet*. FR-41 exists for that sentence, so every row
carries `matured` and the outcome is `null` until it is real.

**No accuracy figure is computed here** (M6-FR-17). Precision over a queue of
unmatured items is not a low-confidence estimate, it is a category error. The
queue links to /metrics, which computes over matured predictions only.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api import sessions
from bellwether import metrics

router = APIRouter()

# Built once at import rather than per route definition. Calling a dependency
# factory inside an argument default rebuilds it on every reload and hides the
# fact that these are two distinct policies — reading the queue and judging an
# item are different permissions and are declared as such here.
ANY_SIGNED_IN = Depends(sessions.require_role("viewer", "reviewer", "admin"))
CAN_JUDGE = Depends(sessions.require_role("reviewer", "admin"))
CSRF = Depends(sessions.check_csrf)

# Ranked by the SERVING champion, which is what the decision log promoted —
# not the most recently trained model (M6-FR-12). A rollback therefore changes
# this queue on the next scoring cycle without anything here being told.
#
# role = 'champion' only. A challenger in shadow writes a second score for every
# event, and showing both would put two numbers beside one edit with nothing to
# say which is being served (M6-FR-13).
QUEUE_SQL = """
WITH serving AS (
    SELECT model_version FROM decide.champion_history
     ORDER BY effective_from DESC, history_id DESC LIMIT 1
),
observed AS (
    SELECT c.revid, max(c.age_seconds) AS last_observed_age,
           bool_or(c.had_reverted_tag) AS ever_positive
      FROM outcome.label_checks c GROUP BY c.revid
)
SELECT p.revid,
       p.score,
       p.model_version,
       e.event_ts,
       e.title,
       e.user_name,
       e.ns,
       (e.is_anon OR e.is_temp)                     AS is_logged_out,
       COALESCE(e.newlen, 0) - COALESCE(e.oldlen, 0) AS byte_delta,
       e.comment,
       -- The M4 rule, unchanged: elapsed time since the EDIT applied to both
       -- classes alike, AND the outcome actually determined. Anything else
       -- lets a fast revert count as matured while a quiet edit never does.
       (EXTRACT(epoch FROM now() - p.event_ts) >= %(maturity)s
        AND (o.ever_positive OR o.last_observed_age >= %(maturity)s)) AS matured,
       o.ever_positive
        OR EXISTS (SELECT 1 FROM outcome.labels l
                    WHERE l.revid = p.revid AND l.label)              AS reverted,
       hl.verdict                                   AS my_verdict,
       hl.judged_at                                 AS my_judged_at
  FROM register.predictions p
  JOIN serving s ON s.model_version = p.model_version
  JOIN landing.rc_events e ON e.revid = p.revid
  LEFT JOIN observed o  ON o.revid = p.revid
  LEFT JOIN app.human_labels hl
         ON hl.revid = p.revid AND hl.user_id = app.acting_user()
 WHERE p.role = 'champion'
   AND p.event_ts >= now() - make_interval(hours => %(hours)s)
 ORDER BY p.score DESC, p.event_ts DESC
 LIMIT %(limit)s
"""

# Published beside the queue so a reader can see how far behind scoring is
# without inferring it from timestamps.
FRESHNESS_SQL = """
SELECT max(p.scored_at)  AS last_scored_at,
       max(e.event_ts)   AS newest_event,
       count(*)          AS scored_in_window
  FROM register.predictions p
  JOIN landing.rc_events e ON e.revid = p.revid
 WHERE p.role = 'champion'
   AND p.event_ts >= now() - make_interval(hours => %(hours)s)
"""

INSERT_LABEL_SQL = """
INSERT INTO app.human_labels
    (revid, user_id, verdict, confidence, champion_version, score_shown, was_matured)
VALUES (%(revid)s, %(user_id)s, %(verdict)s, %(confidence)s, %(champion)s,
        %(score)s, %(matured)s)
ON CONFLICT (revid, user_id) DO NOTHING
RETURNING label_id
"""

# What the reviewer was actually looking at, read back rather than trusted from
# the request. A client that reported its own score_shown could record a
# judgement against a number nobody was ever shown.
SHOWN_SQL = """
WITH observed AS (
    SELECT c.revid, max(c.age_seconds) AS last_observed_age,
           bool_or(c.had_reverted_tag) AS ever_positive
      FROM outcome.label_checks c WHERE c.revid = %(revid)s GROUP BY c.revid
)
SELECT p.score, p.model_version,
       (EXTRACT(epoch FROM now() - p.event_ts) >= %(maturity)s
        AND (o.ever_positive OR o.last_observed_age >= %(maturity)s)) AS matured
  FROM register.predictions p
  LEFT JOIN observed o ON o.revid = p.revid
 WHERE p.revid = %(revid)s AND p.role = 'champion'
 ORDER BY p.scored_at DESC
 LIMIT 1
"""


# Four times the maturity window, so a reviewer can look back far enough to see
# how their judgements turned out. The default stays a day: triage is about what
# just happened.
MAX_WINDOW_HOURS = 720


class Judgement(BaseModel):
    revid: int
    verdict: str = Field(pattern="^(bad_edit|good_edit|unsure)$")
    confidence: str = Field(pattern="^(low|medium|high)$")


@router.get("/queue")
def queue(
    request: Request,
    hours: int = 24,
    limit: int = 50,
    user: dict[str, Any] = ANY_SIGNED_IN,
) -> dict[str, Any]:
    """Recent edits ranked by the serving champion's score."""
    # The ceiling must exceed the maturity window, or no matured item can ever
    # appear and `matured` is a constant false — the marker FR-41 requires would
    # distinguish nothing, and a reviewer could never see the outcome of an edit
    # they judged. The first version capped at 168 hours, which IS the maturity
    # window.
    hours = max(1, min(hours, MAX_WINDOW_HOURS))
    limit = max(1, min(limit, 200))

    with sessions.acting_connection(user) as conn:
        rows = conn.execute(
            QUEUE_SQL,
            {"maturity": metrics.PROVISIONAL_MATURITY_SECONDS, "hours": hours, "limit": limit},
        ).fetchall()
        freshness = conn.execute(FRESHNESS_SQL, {"hours": hours}).fetchone() or {}

    items = []
    for row in rows:
        matured = bool(row["matured"])
        items.append(
            {
                "revid": row["revid"],
                "score": round(float(row["score"]), 4),
                "model_version": row["model_version"],
                "event_ts": row["event_ts"],
                "title": row["title"],
                "user_name": row["user_name"],
                "namespace": row["ns"],
                "is_logged_out": row["is_logged_out"],
                "byte_delta": row["byte_delta"],
                "comment": row["comment"],
                "matured": matured,
                # Null until it is real. A false here would read as "this edit
                # survived", which is not the same as "nobody has checked".
                "reverted": bool(row["reverted"]) if matured else None,
                "my_verdict": row["my_verdict"],
                "my_judged_at": row["my_judged_at"],
            }
        )

    return {
        "items": items,
        "window_hours": hours,
        "matured": sum(1 for i in items if i["matured"]),
        "immature": sum(1 for i in items if not i["matured"]),
        "maturity_hours": metrics.PROVISIONAL_MATURITY_SECONDS // 3600,
        "freshness": {
            "last_scored_at": freshness.get("last_scored_at"),
            "newest_event": freshness.get("newest_event"),
            "scored_in_window": freshness.get("scored_in_window", 0),
        },
        "note": (
            "Ranked by the champion the decision log promoted. Most items are "
            "immature: their outcome is not final and `reverted` is null, which is "
            "what a triage queue is — a score here is what the model expects, not "
            "what happened. No accuracy figure is computed over this list; see "
            "/metrics, which uses matured predictions only."
        ),
    }


@router.post("/labels")
def record_label(
    request: Request,
    judgement: Judgement,
    user: dict[str, Any] = CAN_JUDGE,
    _csrf: None = CSRF,
) -> dict[str, Any]:
    """Record what a reviewer thought (SRS FR-42, M6-FR-18 to FR-20).

    The champion and the score are read from the database, not taken from the
    request. A judgement is partly a reaction to the number on the screen, and a
    client that supplied its own could file an opinion against a score nobody
    was ever shown.
    """
    with sessions.acting_connection(user) as conn:
        shown = conn.execute(
            SHOWN_SQL,
            {"revid": judgement.revid, "maturity": metrics.PROVISIONAL_MATURITY_SECONDS},
        ).fetchone()

        if not shown:
            sessions.audit(
                conn,
                actor=user["user_id"],
                actor_role=user["role"],
                action="record_label",
                outcome="refused",
                target=str(judgement.revid),
                detail="no champion prediction for that revision",
            )
            conn.commit()
            raise HTTPException(status_code=404, detail="That revision is not in the queue.")

        row = conn.execute(
            INSERT_LABEL_SQL,
            {
                "revid": judgement.revid,
                "user_id": user["user_id"],
                "verdict": judgement.verdict,
                "confidence": judgement.confidence,
                "champion": shown["model_version"],
                "score": float(shown["score"]),
                "matured": bool(shown["matured"]),
            },
        ).fetchone()

        # ON CONFLICT DO NOTHING returns nothing when this reviewer has already
        # judged this edit. Not an error — a double-submitted form, or two tabs
        # — but it must not read as a second judgement.
        already = row is None
        sessions.audit(
            conn,
            actor=user["user_id"],
            actor_role=user["role"],
            action="record_label",
            outcome="allowed",
            target=str(judgement.revid),
            detail=f"{judgement.verdict}/{judgement.confidence}"
            + (" (already judged)" if already else ""),
        )
        conn.commit()

    return {
        "recorded": not already,
        "already_judged": already,
        "revid": judgement.revid,
        "score_shown": round(float(shown["score"]), 4),
        "was_matured": bool(shown["matured"]),
    }
