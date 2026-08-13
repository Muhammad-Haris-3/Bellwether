"""Parser tests.

These use the exact shapes formatversion=2 emits, including the awkward one:
boolean flags are present-when-true and absent-when-false. Reading them as
required keys passes on a hand-written fixture with every flag set and fails on
the majority of real rows.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from bellwether.http import MediaWikiClient
from bellwether.mediawiki import (
    iter_recent_changes,
    parse_recent_change,
    parse_timestamp,
)

API = "https://en.wikipedia.org/w/api.php"


def test_parse_timestamp_is_utc_aware() -> None:
    ts = parse_timestamp("2026-08-13T10:20:30Z")
    assert ts == datetime(2026, 8, 13, 10, 20, 30, tzinfo=UTC)
    assert ts.tzinfo is not None


def test_registered_edit_has_flags_absent() -> None:
    raw = {
        "type": "edit",
        "ns": 0,
        "title": "Cricket",
        "pageid": 12,
        "revid": 900,
        "old_revid": 899,
        "rcid": 77,
        "user": "SomeEditor",
        "userid": 4242,
        "oldlen": 5000,
        "newlen": 5120,
        "comment": "expand lead",
        "timestamp": "2026-08-13T10:20:30Z",
        "tags": [],
    }
    event = parse_recent_change(raw)
    assert event["is_anon"] is False
    assert event["is_minor"] is False
    assert event["is_bot"] is False
    assert event["user_id"] == 4242
    assert event["tags"] == []


def test_temporary_account_edit_is_flagged_temp_not_anon() -> None:
    """A real row, copied from the live API on 2026-08-13.

    English Wikipedia has IP masking: logged-out editors are given temporary
    accounts and the `anon` flag never appears. This row is the reason the
    sampling frame is keyed on logged-out-ness rather than on `anon`.
    """
    raw = {
        "type": "edit",
        "ns": 0,
        "title": "Ty Gibbs",
        "pageid": 61064605,
        "revid": 1369185025,
        "old_revid": 1368764905,
        "rcid": 2057113478,
        "user": "~2026-44334-20",
        "userid": 55204706,
        "temp": True,
        "bot": False,
        "new": False,
        "minor": False,
        "oldlen": 82848,
        "newlen": 82848,
        "timestamp": "2026-08-13T10:47:29Z",
        "comment": "/* */",
        "tags": ["mobile edit", "mobile web edit"],
    }
    event = parse_recent_change(raw)
    assert event["is_temp"] is True
    assert event["is_anon"] is False
    assert event["user_id"] == 55204706


def test_anonymous_minor_edit_has_flags_present() -> None:
    raw = {
        "type": "edit",
        "ns": 0,
        "title": "Karachi",
        "revid": 901,
        "old_revid": 900,
        "rcid": 78,
        "user": "203.0.113.9",
        "userid": 0,
        "anon": True,
        "minor": True,
        "oldlen": 5120,
        "newlen": 5100,
        "comment": "",
        "timestamp": "2026-08-13T10:21:00Z",
        "tags": ["mobile edit"],
    }
    event = parse_recent_change(raw)
    assert event["is_anon"] is True
    assert event["is_temp"] is False
    assert event["is_minor"] is True
    assert event["user_name"] == "203.0.113.9"
    assert event["tags"] == ["mobile edit"]


def test_revision_deleted_row_is_kept_not_dropped() -> None:
    """A suppressed summary is still a real edit.

    Dropping these would bias the sample toward the unremarkable, which is
    precisely the wrong direction for a model that ranks by risk.
    """
    raw = {
        "type": "edit",
        "ns": 0,
        "title": "Somewhere",
        "revid": 902,
        "old_revid": 901,
        "rcid": 79,
        "userhidden": True,
        "commenthidden": True,
        "timestamp": "2026-08-13T10:22:00Z",
    }
    event = parse_recent_change(raw)
    assert event["comment_hidden"] is True
    assert event["user_hidden"] is True
    assert event["user_name"] is None
    assert event["tags"] == []


def _rc_payload(revids: list[int], cont: str | None) -> dict:
    body: dict = {
        "query": {
            "recentchanges": [
                {
                    "type": "edit",
                    "ns": 0,
                    "title": f"Page {r}",
                    "revid": r,
                    "old_revid": r - 1,
                    "rcid": r,
                    "user": "Someone",
                    "userid": 1,
                    "oldlen": 10,
                    "newlen": 20,
                    "comment": "c",
                    "timestamp": f"2026-08-13T10:{r % 60:02d}:00Z",
                    "tags": [],
                }
                for r in revids
            ]
        }
    }
    if cont:
        body["continue"] = {"rccontinue": cont, "continue": "-||"}
    return body


@respx.mock
def test_paging_follows_continue_then_stops() -> None:
    route = respx.get(API)
    route.side_effect = [
        httpx.Response(200, json=_rc_payload([1, 2], "20260813102000|2")),
        httpx.Response(200, json=_rc_payload([3, 4], None)),
    ]

    with MediaWikiClient(per_minute=100_000) as client:
        pages = list(
            iter_recent_changes(client, start=datetime(2026, 8, 13, 10, tzinfo=UTC), max_pages=10)
        )

    assert [e["revid"] for page in pages for e in page] == [1, 2, 3, 4]
    assert client.calls == 2


@respx.mock
def test_paging_respects_max_pages() -> None:
    """A run that hits the cap must stop, not run until the workflow is killed.

    Being killed mid-page would leave the cursor behind the rows already
    committed, and the next run would re-read them — harmless but wasteful, and
    it would hide the fact that ingestion is not keeping up.
    """
    respx.get(API).mock(return_value=httpx.Response(200, json=_rc_payload([1], "always-more")))

    with MediaWikiClient(per_minute=100_000) as client:
        pages = list(
            iter_recent_changes(client, start=datetime(2026, 8, 13, tzinfo=UTC), max_pages=3)
        )

    assert len(pages) == 3
    assert client.calls == 3


@respx.mock
def test_api_error_object_is_raised_not_swallowed() -> None:
    """The Action API answers a bad query with HTTP 200 and an error object.

    Treating that as success is how a job reports a clean run having written
    nothing at all.
    """
    from bellwether.http import UpstreamError

    respx.get(API).mock(
        return_value=httpx.Response(
            200, json={"error": {"code": "permissiondenied", "info": "no patrol right"}}
        )
    )

    with (
        MediaWikiClient(per_minute=100_000) as client,
        pytest.raises(UpstreamError, match="permissiondenied"),
    ):
        client.get({"action": "query"})


def test_patrolled_is_not_requested() -> None:
    """rcprop=patrolled needs the patrol right and 403s an anonymous client.

    Not a degraded field — a failed call. This test exists because requesting
    it would break every single request, and the failure would look like a
    network problem.
    """
    from bellwether.mediawiki import RC_PROPS

    assert "patrolled" not in RC_PROPS
