"""One-command database setup for a fresh Postgres (Neon, or anything else).

Takes the owner connection string and does everything M0 needs:

    1. connects, and reports what it actually connected to
    2. applies sql/001 and sql/002
    3. gives bellwether_writer and bellwether_readonly a login and a password
    4. VERIFIES the append-only guarantee holds on THIS server, by trying the
       forbidden operations and requiring them to be refused
    5. prints the two connection strings to paste into GitHub and Render

Step 4 is the reason this script exists rather than a README section. A grant
that was supposed to be applied and silently was not looks exactly like one
that was, right up until the moment it matters.

Everything is idempotent, so re-running after a failure is safe and expected.

Usage:

    python scripts/bootstrap_database.py "postgresql://owner:pw@host/db?sslmode=require"

Passwords are generated and printed once. Nothing is written to disk. Re-running
does NOT rotate a deployed password unless --rotate-passwords is passed.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

REPO = Path(__file__).resolve().parent.parent
SQL_DIR = REPO / "sql"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

WRITER = "bellwether_writer"
READER = "bellwether_readonly"


def ok(msg: str) -> None:
    print(f"  {GREEN}OK{RESET}   {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}FAIL{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}WARN{RESET} {msg}")


def step(n: int, title: str) -> None:
    print(f"\n{'=' * 70}\nSTEP {n} - {title}\n{'=' * 70}")


def swap_credentials(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit(
        (parts.scheme, f"{user}:{password}@{host}{port}", parts.path, parts.query, "")
    )


def role_has_login(conn: psycopg.Connection, role: str) -> bool:
    row = conn.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    return bool(row and row[0])


def ensure_login(conn: psycopg.Connection, role: str, password: str) -> None:
    conn.execute(
        sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)
        )
    )


def verify_guarantees(url: str, writer_url: str, reader_url: str) -> bool:
    """Try the forbidden operations. Every one must be refused.

    Uses the real login roles rather than SET ROLE: SET ROLE proves the grant
    exists, but only a genuine connection proves the credential that will
    actually be deployed is the one carrying it.
    """
    all_good = True

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO landing.rc_events
                (revid, event_ts, ns, title, is_anon, is_temp, is_minor, is_bot,
                 sampling_stratum, ingested_at_utc)
            VALUES (-1, now(), 0, 'bootstrap probe', false, false, false, false,
                    'registered', now())
            ON CONFLICT (revid) DO NOTHING
            """
        )
        conn.execute(
            """
            INSERT INTO outcome.labels
                (revid, label, label_source, first_observed_at_utc, detection_latency_seconds)
            VALUES (-1, true, 'mw_reverted', now(), 1)
            ON CONFLICT (revid, label_source) DO NOTHING
            """
        )

    forbidden = [
        (writer_url, WRITER, "UPDATE outcome.labels SET label = false WHERE revid = -1"),
        (writer_url, WRITER, "DELETE FROM outcome.labels WHERE revid = -1"),
        (writer_url, WRITER, "DELETE FROM landing.rc_events WHERE revid = -1"),
        (writer_url, WRITER, "DELETE FROM outcome.label_checks WHERE revid = -1"),
        (reader_url, READER, "DELETE FROM landing.rc_events WHERE revid = -1"),
        (
            reader_url,
            READER,
            "INSERT INTO landing.cursors (job, position_utc) VALUES ('probe', now())",
        ),
    ]

    for dsn, role, statement in forbidden:
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(statement)  # type: ignore[arg-type]
        except psycopg.errors.InsufficientPrivilege:
            ok(f"{role} refused: {statement[:58]}")
        except Exception as exc:  # noqa: BLE001
            warn(f"{role}: unexpected {type(exc).__name__} on {statement[:40]} - {exc}")
            all_good = False
        else:
            fail(f"{role} was ALLOWED to run: {statement}")
            all_good = False

    # The writer must still be able to do its job.
    try:
        with psycopg.connect(writer_url, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO landing.cursors (job, position_utc) VALUES ('probe', now()) "
                "ON CONFLICT (job) DO UPDATE SET position_utc = EXCLUDED.position_utc"
            )
        ok(f"{WRITER} can still move the cursor")
    except Exception as exc:  # noqa: BLE001
        fail(f"{WRITER} cannot move the cursor: {exc}")
        all_good = False

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DELETE FROM outcome.labels WHERE revid = -1")
        conn.execute("DELETE FROM landing.rc_events WHERE revid = -1")
        conn.execute("DELETE FROM landing.cursors WHERE job = 'probe'")

    return all_good


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database_url", help="Owner connection string (the DIRECT one, not pooled)")
    parser.add_argument(
        "--rotate-passwords",
        action="store_true",
        help="Set new passwords even if the roles already have a login. "
        "Will break a deployed service until the new strings are pasted in.",
    )
    args = parser.parse_args()
    url = args.database_url

    step(1, "Connect")
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(
            "SELECT current_database() AS db, current_user AS usr, version() AS v"
        ).fetchone()
    assert row is not None
    ok(f"database {row[0]} as {row[1]}")
    print(f"  {DIM}{row[2][:70]}{RESET}")

    step(2, "Apply schema and grants")
    with psycopg.connect(url, autocommit=True) as conn:
        for path in sorted(SQL_DIR.glob("*.sql")):
            conn.execute(path.read_text(encoding="utf-8").encode("utf-8"))
            ok(f"applied {path.name}")

    step(3, "Role logins")
    writer_pw = secrets.token_urlsafe(24)
    reader_pw = secrets.token_urlsafe(24)
    with psycopg.connect(url, autocommit=True) as conn:
        for role, pw in ((WRITER, writer_pw), (READER, reader_pw)):
            if role_has_login(conn, role) and not args.rotate_passwords:
                warn(f"{role} already has a login; password left alone")
                warn("  re-run with --rotate-passwords to change it")
            else:
                ensure_login(conn, role, pw)
                ok(f"{role} login set")

    writer_url = swap_credentials(url, WRITER, writer_pw)
    reader_url = swap_credentials(url, READER, reader_pw)

    step(4, "Verify the append-only guarantee ON THIS SERVER")
    if not verify_guarantees(url, writer_url, reader_url):
        print(f"\n{RED}The guarantee does not hold. Do not deploy this database.{RESET}")
        return 1
    ok("every forbidden operation was refused")

    step(5, "Connection strings")
    print(f"\n  {DIM}GitHub Actions secret BELLWETHER_DATABASE_URL:{RESET}")
    print(f"  {writer_url}")
    print(f"\n  {DIM}Render environment BELLWETHER_READONLY_DATABASE_URL:{RESET}")
    print(f"  {reader_url}")
    print(
        f"\n  {YELLOW}The writer string never goes near the serving container.{RESET}\n"
        f"  {DIM}If the API held write credentials, the append-only guarantee "
        f"would rest on\n  the API choosing not to use them.{RESET}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
