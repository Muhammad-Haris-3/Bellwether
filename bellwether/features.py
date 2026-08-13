"""Point-in-time features (M2 §3, SRS FR-12 to FR-15).

Every feature here is computable from the event itself plus history Bellwether
had already ingested when that event happened. Nothing calls the API at
scoring time, and nothing reads a column whose value can change after the fact.

**The tag trap.** `rc_events.tags` is frozen at ingestion, and rows backfilled
days later carry `mw-reverted` in it while live rows structurally never can. A
feature derived from tags without excluding outcome tags would be a perfect
predictor on 40% of the current data and worthless on the rest — scoring
superbly in any backtest that included the backfill, and failing silently on
deployment. `FORBIDDEN_TAGS` exists for that, and
:mod:`bellwether.knowability` proves it holds rather than trusting it.

Feature functions take `(event, history)` and must be pure. Purity is what
makes the guard's differential probes meaningful: if a function can consult
anything not passed to it, mutating its inputs proves nothing.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from datetime import datetime
from typing import Any

# Tags that describe the OUTCOME of an edit rather than the edit itself.
#
# `mw-reverted` is applied to an edit *after* somebody reverts it. It is the
# label. Anything derived from it is the answer, not a feature.
#
# The reverting tags — mw-undo, mw-rollback, mw-manual-revert — are NOT
# forbidden: they describe what this edit did, are applied when it is saved,
# and are knowable at scoring time.
FORBIDDEN_TAGS = frozenset({"mw-reverted"})

# Columns a feature must never read, because their value depends on when
# Bellwether happened to fetch the row rather than on anything about the edit.
FORBIDDEN_COLUMNS = frozenset({"ingested_at_utc", "ingest_run_id"})

History = dict[str, Any]
FeatureFn = Callable[[dict[str, Any], History], float]


def _safe_tags(event: dict[str, Any]) -> list[str]:
    """Tags with outcome tags removed. The only sanctioned way in."""
    return [t for t in (event.get("tags") or []) if t not in FORBIDDEN_TAGS]


def _byte_delta(event: dict[str, Any]) -> int:
    return int(event.get("newlen") or 0) - int(event.get("oldlen") or 0)


def _hour(event: dict[str, Any]) -> int:
    ts = event["event_ts"]
    return ts.hour if isinstance(ts, datetime) else 0


def _weekday(event: dict[str, Any]) -> int:
    ts = event["event_ts"]
    return ts.weekday() if isinstance(ts, datetime) else 0


def _comment(event: dict[str, Any]) -> str:
    return event.get("comment") or ""


# --- the feature set -------------------------------------------------------
#
# Ordered by group, matching M2 §3.4. Each is a pure function of the event and
# the history view; none consults the network, the clock, or a column in
# FORBIDDEN_COLUMNS.
EVENT_FEATURES: dict[str, FeatureFn] = {
    # Editor class. The logged-out flag alone is the KC-2 baseline: 22% against
    # 3.3%. Anything the model adds is measured against it, not against zero.
    "is_logged_out": lambda e, h: float(bool(e.get("is_anon") or e.get("is_temp"))),
    "is_temp_account": lambda e, h: float(bool(e.get("is_temp"))),
    # user_id rises monotonically with account creation, so its magnitude is an
    # account-age proxy. It was fixed before the edit existed and cannot leak.
    # log1p because the range spans eight orders of magnitude.
    "log_user_id": lambda e, h: math.log1p(float(e.get("user_id") or 0)),
    "is_minor": lambda e, h: float(bool(e.get("is_minor"))),
    # Size.
    "byte_delta": lambda e, h: float(_byte_delta(e)),
    "abs_byte_delta": lambda e, h: float(abs(_byte_delta(e))),
    "is_new_page": lambda e, h: float(not e.get("old_revid")),
    # Blanking most of a page is a classic vandalism shape and is knowable from
    # the two length fields alone.
    "removed_most_of_page": lambda e, h: float(
        bool(e.get("oldlen")) and _byte_delta(e) < -0.8 * float(e.get("oldlen") or 1)
    ),
    # Edit summary. Its presence and shape, never its text.
    "has_comment": lambda e, h: float(bool(_comment(e).strip())),
    "comment_length": lambda e, h: float(len(_comment(e))),
    "is_section_edit": lambda e, h: float("/*" in _comment(e)),
    "comment_has_link": lambda e, h: float("http" in _comment(e).lower()),
    "comment_hidden": lambda e, h: float(bool(e.get("comment_hidden"))),
    # Tooling. Outcome tags are stripped by _safe_tags before counting.
    "tag_count": lambda e, h: float(len(_safe_tags(e))),
    "is_mobile": lambda e, h: float(any("mobile" in t for t in _safe_tags(e))),
    "is_visual_editor": lambda e, h: float("visualeditor" in _safe_tags(e)),
    # This edit is itself a revert. Applied when it is saved, so knowable.
    "is_reverting": lambda e, h: float(
        any(t in {"mw-undo", "mw-rollback", "mw-manual-revert"} for t in _safe_tags(e))
    ),
    # Time, cyclically encoded so 23:00 and 00:00 are adjacent.
    "hour_sin": lambda e, h: math.sin(2 * math.pi * _hour(e) / 24),
    "hour_cos": lambda e, h: math.cos(2 * math.pi * _hour(e) / 24),
    "weekday_sin": lambda e, h: math.sin(2 * math.pi * _weekday(e) / 7),
    "weekday_cos": lambda e, h: math.cos(2 * math.pi * _weekday(e) / 7),
}

# History-derived features. Populated in M2-T2 once editor_state exists; the
# guard already covers them, so they cannot be added without being checked.
HISTORY_FEATURES: dict[str, FeatureFn] = {}


def feature_names() -> list[str]:
    return sorted({*EVENT_FEATURES, *HISTORY_FEATURES})


def build(event: dict[str, Any], history: History | None = None) -> dict[str, float]:
    """Compute the feature vector for one event."""
    hist: History = history or {}
    vector: dict[str, float] = {}
    for name in feature_names():
        fn = EVENT_FEATURES.get(name) or HISTORY_FEATURES[name]
        vector[name] = float(fn(event, hist))
    return vector


def feature_hash(vector: dict[str, float]) -> str:
    """A stable digest of a feature vector (SRS FR-15).

    Sorted keys and repr-free float formatting, so the same inputs give the
    same hash on any machine — which is what lets a historical prediction be
    reproduced and checked rather than taken on trust.
    """
    payload = json.dumps({k: round(v, 10) for k, v in sorted(vector.items())}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
