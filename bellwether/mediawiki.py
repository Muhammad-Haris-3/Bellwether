"""Parsing and paging for the two Action API queries this project uses.

Kept separate from :mod:`bellwether.http` so that the wire behaviour (retries,
rate limiting, headers) and the shape of the data can be tested independently —
the parsers here have no network dependency at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from bellwether.http import MediaWikiClient

# Requested properties for the event feed.
#
# `patrolled` is deliberately ABSENT. The Action API requires the `patrol` or
# `patrolmarks` right to return it and answers an anonymous request for it with
# permissiondenied — so including it would fail every call, not degrade one
# field. The column exists in the schema and stays NULL until there is a reason
# and a means to fill it.
RC_PROPS = "ids|timestamp|title|user|userid|comment|flags|sizes|tags"

# Tags applied to the edit that DID the reverting (SRS 6.4, secondary path).
REVERTING_TAGS = frozenset({"mw-undo", "mw-rollback", "mw-manual-revert"})

# The tag applied to the edit that WAS reverted (SRS 6.4, primary path).
REVERTED_TAG = "mw-reverted"

# The Action API accepts 50 ids per request for clients without apihighlimits.
REVIDS_PER_REQUEST = 50


def parse_timestamp(value: str) -> datetime:
    """Parse an Action API ISO-8601 timestamp into an aware UTC datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def parse_recent_change(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise one recentchanges row.

    formatversion=2 emits boolean flags by presence: ``"minor": true`` appears
    when the flag is set and the key is simply absent when it is not. Reading
    them with ``.get(..., False)`` is therefore correct, and reading them as
    ``raw["minor"]`` would raise on the majority of rows.

    Revision-deleted fields behave the same way: ``comment`` disappears and
    ``commenthidden`` appears. Those rows are kept rather than dropped — an
    edit whose summary was suppressed is a real event, and quietly discarding
    it would bias the sample toward the unremarkable.
    """
    return {
        "revid": raw["revid"],
        "old_revid": raw.get("old_revid") or None,
        "rcid": raw.get("rcid"),
        "event_ts": parse_timestamp(raw["timestamp"]),
        "ns": raw["ns"],
        "title": raw["title"],
        "user_name": raw.get("user"),
        "user_id": raw.get("userid"),
        "is_anon": bool(raw.get("anon", False)),
        # Temporary accounts (IP masking). A logged-out edit on English
        # Wikipedia no longer carries `anon` and no longer shows an IP address:
        # it is attributed to an auto-created account named like
        # "~2026-44334-20" and flagged `temp`. Measured 2026-08-13: 0 of 2,498
        # main-namespace edits had `anon`; 13% had `temp`.
        #
        # Both flags are kept. `anon` still occurs on wikis without temporary
        # accounts and on historical data, and collapsing them at ingestion
        # would destroy the distinction rather than record it.
        "is_temp": bool(raw.get("temp", False)),
        "is_minor": bool(raw.get("minor", False)),
        "is_bot": bool(raw.get("bot", False)),
        "comment": raw.get("comment"),
        "comment_hidden": bool(raw.get("commenthidden", False)),
        "user_hidden": bool(raw.get("userhidden", False)),
        "oldlen": raw.get("oldlen"),
        "newlen": raw.get("newlen"),
        "tags": list(raw.get("tags", [])),
    }


def iter_recent_changes(
    client: MediaWikiClient,
    *,
    start: datetime,
    limit: int = 500,
    max_pages: int = 40,
) -> Iterator[list[dict[str, Any]]]:
    """Yield pages of main-namespace, non-bot edits from ``start`` forward.

    ``rcdir=newer`` enumerates oldest-first, which is what makes incremental
    ingestion safe: each page can be committed and the cursor advanced before
    the next is requested, so an interrupted run leaves a shorter history
    rather than a hole in the middle of one.

    ``start`` is inclusive. See the comment on ``landing.cursors`` — one-second
    timestamp resolution means an exclusive cursor could skip an edit that
    shared its second with the last one committed.
    """
    params: dict[str, Any] = {
        "action": "query",
        "list": "recentchanges",
        "formatversion": 2,
        "rcnamespace": 0,
        "rctype": "edit",
        "rcshow": "!bot",
        "rcprop": RC_PROPS,
        "rclimit": limit,
        "rcdir": "newer",
        "rcstart": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    for _ in range(max_pages):
        body = client.get(params)
        rows = body.get("query", {}).get("recentchanges", [])
        if rows:
            yield [parse_recent_change(row) for row in rows]

        cont = body.get("continue")
        if not cont:
            return
        params.update(cont)


def fetch_revision_tags(
    client: MediaWikiClient,
    revids: list[int],
) -> tuple[dict[int, dict[str, Any]], set[int]]:
    """Fetch current tags for specific revisions.

    Returns ``(found, missing)``. A revision goes into ``missing`` when the API
    reports it as a bad revid, which happens when the page or revision has been
    deleted since ingestion. That is an outcome, not an error: a deleted
    revision cannot be labelled, and pretending otherwise would silently drop
    a non-random slice of the sample.
    """
    found: dict[int, dict[str, Any]] = {}
    missing: set[int] = set()

    for i in range(0, len(revids), REVIDS_PER_REQUEST):
        batch = revids[i : i + REVIDS_PER_REQUEST]
        body = client.get(
            {
                "action": "query",
                "prop": "revisions",
                "formatversion": 2,
                "revids": "|".join(str(r) for r in batch),
                "rvprop": "ids|timestamp|tags|flags",
            }
        )

        query = body.get("query", {})
        missing.update(int(r) for r in query.get("badrevids", {}))

        for page in query.get("pages", []):
            for rev in page.get("revisions", []):
                found[int(rev["revid"])] = {
                    "revid": int(rev["revid"]),
                    "parentid": rev.get("parentid"),
                    "timestamp": parse_timestamp(rev["timestamp"]),
                    "tags": list(rev.get("tags", [])),
                    "page_title": page.get("title"),
                    "pageid": page.get("pageid"),
                }

        # Anything we asked for and did not hear about either way. Treated as
        # missing rather than assumed intact, so the count is visible.
        heard = {r for r in batch if r in found} | {r for r in batch if r in missing}
        missing.update(set(batch) - heard)

    return found, missing
