"""Retention and sealing.

Deletion is the only irreversible thing this system does. These tests are about
what it refuses to delete.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Json

from bellwether import auth, retention, seal
from bellwether.db import connect

pytestmark = pytest.mark.db


def _event(conn: Any, revid: int, days_ago: float, *, cohort: bool = False) -> None:
    conn.execute(
        """
        INSERT INTO landing.rc_events
            (revid, event_ts, ns, title, is_anon, is_temp, is_minor, is_bot,
             sampling_stratum, sampling_weight, in_maturity_cohort, ingested_at_utc)
        VALUES (%s, now() - make_interval(days => %s), 0, 'Page',
                false, false, false, false, 'registered', 2.0, %s, now())
        """,
        (revid, days_ago, cohort),
    )


def _label(conn: Any, revid: int, when: datetime) -> None:
    conn.execute(
        """
        INSERT INTO outcome.labels
            (revid, label, label_source, first_observed_at_utc, detection_latency_seconds)
        VALUES (%s, true, 'mw_reverted', %s, 3600)
        """,
        (revid, when),
    )


def _check(conn: Any, revid: int, days_ago: float, *, cohort: bool) -> None:
    conn.execute(
        """
        INSERT INTO outcome.label_checks
            (revid, checkpoint_seconds, checked_at_utc, age_seconds,
             had_reverted_tag, in_maturity_cohort)
        VALUES (%s, 3600, now() - make_interval(days => %s), 3600, false, %s)
        """,
        (revid, days_ago, cohort),
    )


def _prune(conn: Any, *, dry_run: bool) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT target, rows_affected FROM landing.prune_expired("
            "p_dry_run => %s, p_raw_days => 30, p_evidence_days => 90, p_cohort_days => 180)",
            (dry_run,),
        )
        return {r["target"]: int(r["rows_affected"]) for r in cur.fetchall()}


def test_a_dry_run_deletes_nothing(fresh_db: None) -> None:
    """The default. A retention job that deletes by default is one bad argument
    away from the worst bug in the system, and the only unrecoverable one."""
    with connect() as conn:
        _event(conn, 1, days_ago=90)
        counted = _prune(conn, dry_run=True)
        remaining = conn.execute("SELECT count(*) AS n FROM landing.rc_events").fetchone()

    assert counted["landing.rc_events"] == 1
    assert remaining is not None and remaining["n"] == 1


def test_the_dry_run_predicts_exactly_what_apply_deletes(fresh_db: None) -> None:
    """B-5. A dry run whose numbers do not match the real one is worse than no
    dry run: it invites trust it has not earned."""
    with connect() as conn:
        for revid in range(1, 6):
            _event(conn, revid, days_ago=90)
        for revid in range(6, 9):
            _event(conn, revid, days_ago=2)

        predicted = _prune(conn, dry_run=True)
        actual = _prune(conn, dry_run=False)

    assert predicted == actual
    assert actual["landing.rc_events"] == 5


def test_recent_raw_events_survive(fresh_db: None) -> None:
    with connect() as conn:
        _event(conn, 1, days_ago=2)
        assert _prune(conn, dry_run=False)["landing.rc_events"] == 0


def test_an_unsealed_month_is_unprunable(fresh_db: None) -> None:
    """M1-FR-9. Forgetting to seal must stop the pruning, not silently destroy
    the thing the seal was supposed to attest to."""
    old = datetime(2020, 5, 17, tzinfo=UTC)
    with connect() as conn:
        _event(conn, 1, days_ago=200)
        _label(conn, 1, old)
        assert _prune(conn, dry_run=False)["outcome.labels"] == 0
        left = conn.execute("SELECT count(*) AS n FROM outcome.labels").fetchone()

    assert left is not None and left["n"] == 1


def test_a_sealed_month_can_be_pruned(fresh_db: None) -> None:
    old = datetime(2020, 5, 17, tzinfo=UTC)
    with connect() as conn:
        _event(conn, 1, days_ago=200)
        _label(conn, 1, old)
        conn.execute(
            "INSERT INTO outcome.seals (month, row_counts, digest) VALUES (%s, '{}'::jsonb, 'x')",
            (date(2020, 5, 1),),
        )
        assert _prune(conn, dry_run=False)["outcome.labels"] == 1


def test_cohort_checks_outlive_the_rest(fresh_db: None) -> None:
    """The cohort IS the survival study. Ageing it out with the ordinary
    checks would delete the data M2 exists to analyse."""
    with connect() as conn:
        _event(conn, 1, days_ago=60, cohort=True)
        _event(conn, 2, days_ago=60, cohort=False)
        _check(conn, 1, days_ago=60, cohort=True)
        _check(conn, 2, days_ago=60, cohort=False)

        _prune(conn, dry_run=False)
        rows = conn.execute("SELECT revid, in_maturity_cohort FROM outcome.label_checks").fetchall()

    assert [r["revid"] for r in rows] == [1]


def test_the_floor_cannot_be_argued_below(fresh_db: None) -> None:
    """A caller may ask for a more conservative cutoff, never a more aggressive
    one. The floors live inside the function, where the caller cannot reach."""
    with connect() as conn:
        _event(conn, 1, days_ago=3)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rows_affected FROM landing.prune_expired("
                "p_dry_run => true, p_raw_days => 0) WHERE target = 'landing.rc_events'"
            )
            row = cur.fetchone()

    # 0 days was requested; the 7-day floor applies, so a 3-day-old row lives.
    assert row is not None and int(row["rows_affected"]) == 0


def test_the_writer_still_cannot_delete_directly(fresh_db: None) -> None:
    """The point of the SECURITY DEFINER indirection. Retention gained the
    ability to prune; the writer gained nothing."""
    with connect() as conn:
        _event(conn, 1, days_ago=90)

    with connect() as conn:
        conn.execute("SET ROLE bellwether_writer")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM landing.rc_events WHERE revid = 1")


def test_the_writer_may_call_the_pruning_function(fresh_db: None) -> None:
    with connect() as conn:
        conn.execute("SET ROLE bellwether_writer")
        conn.execute("SELECT * FROM landing.prune_expired(p_dry_run => true)")


def test_a_seal_digest_changes_when_a_row_changes(fresh_db: None) -> None:
    """B-6. If editing a row left the digest alone, the seal would attest to
    nothing at all."""
    month = date(2020, 5, 1)
    when = datetime(2020, 5, 17, tzinfo=UTC)
    with connect() as conn:
        _event(conn, 1, days_ago=200)
        _label(conn, 1, when)
        first, counts = seal.compute_digest(conn, month)

        conn.execute("UPDATE outcome.labels SET detection_latency_seconds = 9999 WHERE revid = 1")
        second, _ = seal.compute_digest(conn, month)

    # Asserted per table rather than as an exact dict: the sealed set grows
    # with the project, and a test that breaks whenever evidence is ADDED
    # to the seal is a test that discourages sealing things.
    assert counts["outcome.labels"] == 1
    assert "register.predictions" in counts
    assert first != second


def test_a_seal_is_stable_across_recomputation(fresh_db: None) -> None:
    month = date(2020, 5, 1)
    with connect() as conn:
        _event(conn, 1, days_ago=200)
        _event(conn, 2, days_ago=200)
        _label(conn, 1, datetime(2020, 5, 17, tzinfo=UTC))
        _label(conn, 2, datetime(2020, 5, 18, tzinfo=UTC))
        assert seal.compute_digest(conn, month)[0] == seal.compute_digest(conn, month)[0]


def test_sealing_writes_a_committable_file(fresh_db: None, tmp_path: Any) -> None:
    month = date(2020, 5, 1)
    with connect() as conn:
        _event(conn, 1, days_ago=200)
        _label(conn, 1, datetime(2020, 5, 17, tzinfo=UTC))

    payload = seal.seal_month(month, write=False)
    assert payload["algorithm"] == "sha256"
    assert payload["row_counts"]["outcome.labels"] == 1
    assert "register.predictions" in payload["row_counts"]
    assert len(payload["digest"]) == 64
    json.dumps(payload)  # must be serialisable exactly as committed


def test_storage_is_reported_against_the_budget(fresh_db: None) -> None:
    """M1-FR-11. A budget nobody measures against is a number in a document."""
    result = retention.run(dry_run=True)
    assert result["budget_bytes"] == 400_000_000
    assert result["budget_used_pct"] is not None


# ---------------------------------------------------------------------------
# Session pruning (M6-FR-36)
# ---------------------------------------------------------------------------


def _user(conn: Any, n: int) -> Any:
    digest, salt, params = auth.hash_password("pw")
    row = conn.execute(
        "INSERT INTO app.users (email, password_hash, password_salt, kdf_params, role) "
        "VALUES (%s, %s, %s, %s, 'viewer') RETURNING user_id",
        (f"prune-test-{n}@example.test", digest, salt, Json(params)),
    ).fetchone()
    return row["user_id"]


def _session(conn: Any, user_id: Any, *, expires_at: datetime) -> None:
    conn.execute(
        "INSERT INTO app.sessions (user_id, token_hash, created_at, last_seen_at, expires_at) "
        "VALUES (%s, %s, now(), now(), %s)",
        (user_id, secrets.token_bytes(32), expires_at),
    )


def test_expired_sessions_are_deleted(fresh_db: None) -> None:
    """M6-FR-36. Expired sessions must not accumulate."""
    past = datetime.now(UTC) - timedelta(hours=1)
    future = datetime.now(UTC) + timedelta(hours=12)

    with connect() as conn:
        uid = _user(conn, 1)
        _session(conn, uid, expires_at=past)  # should be pruned
        _session(conn, uid, expires_at=future)  # should survive

    with connect() as conn:
        counted = _prune(conn, dry_run=False)

    assert counted["app.sessions"] == 1

    with connect() as conn:
        count = conn.execute("SELECT count(*) AS n FROM app.sessions").fetchone()["n"]
    assert count == 1


def test_session_dry_run_counts_without_deleting(fresh_db: None) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)

    with connect() as conn:
        uid = _user(conn, 1)
        _session(conn, uid, expires_at=past)

    with connect() as conn:
        counted = _prune(conn, dry_run=True)

    with connect() as conn:
        still_there = conn.execute("SELECT count(*) AS n FROM app.sessions").fetchone()["n"]

    assert counted["app.sessions"] == 1
    assert still_there == 1


def test_live_sessions_survive_pruning(fresh_db: None) -> None:
    future = datetime.now(UTC) + timedelta(hours=12)

    with connect() as conn:
        uid = _user(conn, 1)
        _session(conn, uid, expires_at=future)

    with connect() as conn:
        _prune(conn, dry_run=False)

    with connect() as conn:
        count = conn.execute("SELECT count(*) AS n FROM app.sessions").fetchone()["n"]
    assert count == 1


@pytest.mark.db
def test_pruning_covers_every_table_it_is_supposed_to(fresh_db: None) -> None:
    """The guard for a whole class of mistake, not just the one that happened.

    landing.prune_expired has been defined three times: sql/005 created it,
    sql/011 EXTENDED it to prune register.predictions under the seal guard, and
    sql/025 replaced it again to add app.sessions. That last one was written
    from 005's body, so CREATE OR REPLACE silently reverted 011's addition —
    predictions would never have been pruned again and their seal guard would
    have gone with them.

    Nothing about that fails loudly. The function still ran, still reported
    rows, and simply stopped mentioning one table. So this asserts the full set
    of targets rather than any single one: a future replacement that forgets a
    limb fails here instead of quietly shrinking what retention covers.
    """
    with connect() as conn:
        targets = {
            row["target"]
            for row in conn.execute("SELECT * FROM landing.prune_expired(true)").fetchall()
        }

    assert targets == {
        "landing.rc_events",
        "outcome.label_checks",
        "outcome.labels",
        "register.predictions",
        "app.sessions",
    }
