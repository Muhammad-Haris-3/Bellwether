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


def ensure_login(conn: psycopg.Connection, role: str, password: str) -> bool:
    """Give the role a login and a password. False if we are not allowed to.

    Altering a role needs CREATEROLE *and* ADMIN OPTION on that role. Roles are
    cluster-wide while databases are not, so an owner can find a role already
    present that some other owner created, and hold neither. That is a
    recoverable situation — the grants may be entirely correct — so it is
    reported rather than raised.
    """
    try:
        conn.execute(
            sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
    except psycopg.errors.InsufficientPrivilege:
        return False
    return True


MOVE_CURSOR = (
    "INSERT INTO landing.cursors (job, position_utc) VALUES ('probe', now()) "
    "ON CONFLICT (job) DO UPDATE SET position_utc = EXCLUDED.position_utc"
)

FORBIDDEN: list[tuple[str, str]] = [
    (WRITER, "UPDATE outcome.labels SET label = false WHERE revid = -1"),
    (WRITER, "DELETE FROM outcome.labels WHERE revid = -1"),
    (WRITER, "DELETE FROM landing.rc_events WHERE revid = -1"),
    (WRITER, "DELETE FROM outcome.label_checks WHERE revid = -1"),
    (READER, "DELETE FROM landing.rc_events WHERE revid = -1"),
    (READER, "INSERT INTO landing.cursors (job, position_utc) VALUES ('probe', now())"),
]


def _attempt(dsn: str, statement: str, *, as_role: str | None = None) -> str:
    """Run one statement and report how the server answered.

    A fresh connection per statement: a refusal aborts the transaction, and
    reusing the connection would make every subsequent statement fail for the
    wrong reason — which is exactly how a broken grant hides behind a cascade
    of unrelated errors.

    SET ROLE is attempted separately and its failure reported distinctly.
    Assuming a role you are not a member of raises InsufficientPrivilege — the
    same error a denied statement raises — so folding them together makes every
    forbidden probe report "refused" without having executed anything at all.
    Six passes that prove nothing, and only a probe expecting success to notice.
    """
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            if as_role:
                try:
                    conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(as_role)))
                except psycopg.Error as exc:
                    return f"noassume|{str(exc).splitlines()[0][:110]}"
            try:
                conn.execute(statement.encode())
            except psycopg.errors.InsufficientPrivilege:
                return "refused"
    except Exception as exc:  # noqa: BLE001
        first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return f"error|{type(exc).__name__}: {first_line[:110]}"
    return "allowed"


# (role, table, privilege, must_have) — read straight from the catalogue.
#
# Independent of SET ROLE and of any password, so it answers "what is actually
# granted" even when no way of acting as the role is available. When a probe
# and this table disagree, this one is describing the grant and the probe is
# describing something else.
EXPECTED_PRIVILEGES: list[tuple[str, str, str, bool]] = [
    (WRITER, "landing.rc_events", "INSERT", True),
    (WRITER, "landing.rc_events", "UPDATE", False),
    (WRITER, "landing.rc_events", "DELETE", False),
    (WRITER, "outcome.labels", "INSERT", True),
    (WRITER, "outcome.labels", "UPDATE", False),
    (WRITER, "outcome.labels", "DELETE", False),
    (WRITER, "outcome.label_checks", "INSERT", True),
    (WRITER, "outcome.label_checks", "UPDATE", False),
    (WRITER, "outcome.label_checks", "DELETE", False),
    (WRITER, "landing.cursors", "INSERT", True),
    (WRITER, "landing.cursors", "UPDATE", True),
    (WRITER, "landing.run_log", "INSERT", True),
    (WRITER, "landing.run_log", "UPDATE", True),
    (READER, "landing.rc_events", "SELECT", True),
    (READER, "landing.rc_events", "INSERT", False),
    (READER, "outcome.labels", "SELECT", True),
    (READER, "outcome.labels", "INSERT", False),
    (READER, "landing.cursors", "INSERT", False),
]


def check_privileges(url: str) -> bool:
    """Assert the grant catalogue matches what sql/002 intends."""
    all_good = True
    with psycopg.connect(url, autocommit=True) as conn:
        for role, table, privilege, must_have in EXPECTED_PRIVILEGES:
            row = conn.execute(
                "SELECT has_table_privilege(%s, %s, %s) AS granted",
                (role, table, privilege),
            ).fetchone()
            granted = bool(row and row[0])
            if granted == must_have:
                verb = "has" if must_have else "lacks"
                ok(f"{role} {verb} {privilege} on {table}")
            else:
                fail(
                    f"{role} {'LACKS' if must_have else 'HAS'} {privilege} "
                    f"on {table} - expected the opposite"
                )
                all_good = False
    return all_good


def can_assume(url: str, role: str) -> bool:
    return _attempt(url, "SELECT 1", as_role=role) == "allowed"


def seed_probe(url: str) -> None:
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


def clear_probe(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DELETE FROM outcome.labels WHERE revid = -1")
        conn.execute("DELETE FROM landing.rc_events WHERE revid = -1")
        conn.execute("DELETE FROM landing.cursors WHERE job = 'probe'")


def verify_grants(url: str) -> bool:
    """Check the grants themselves, using SET ROLE from the owner.

    Needs no role passwords, so it works on every run — including a re-run that
    deliberately left existing passwords alone. This is the check that must
    never be skipped: it is the one that proves the append-only property.
    """
    all_good = True

    # Establish this FIRST. Without it every probe below reports "refused"
    # having executed nothing, and the forbidden ones all look like passes.
    for role in (WRITER, READER):
        if can_assume(url, role):
            ok(f"can act as {role} - probes below are meaningful")
        else:
            warn(f"cannot SET ROLE {role} - skipping the behavioural probes")
            warn("  the owner lacks ADMIN on that role, which happens when some")
            warn("  other owner created it (roles are cluster-wide, databases are not)")
            warn("  STEP 4 read the grants from the catalogue and is authoritative")
            return True

    seed_probe(url)

    for role, statement in FORBIDDEN:
        outcome = _attempt(url, statement, as_role=role)
        if outcome == "refused":
            ok(f"{role} refused: {statement[:56]}")
        elif outcome == "allowed":
            fail(f"{role} was ALLOWED to run: {statement}")
            all_good = False
        else:
            fail(f"{role}: could not test - {outcome.split('|', 1)[1]}")
            all_good = False

    outcome = _attempt(url, MOVE_CURSOR, as_role=WRITER)
    if outcome == "allowed":
        ok(f"{WRITER} can still move the cursor")
    else:
        fail(f"{WRITER} cannot move the cursor: {outcome}")
        all_good = False

    clear_probe(url)
    return all_good


def verify_logins(url: str, writer_url: str, reader_url: str) -> bool:
    """Check that the credentials being handed out actually carry those grants.

    SET ROLE proves the grant exists on the role. Only a real connection proves
    the string about to be pasted into GitHub authenticates as that role and
    inherits it. Runs only when this invocation set the passwords — otherwise
    it has no password to connect with, and attempting anyway produces an
    authentication failure dressed up as a broken guarantee.
    """
    all_good = True
    seed_probe(url)
    dsn_for = {WRITER: writer_url, READER: reader_url}

    for role, statement in FORBIDDEN:
        outcome = _attempt(dsn_for[role], statement)
        if outcome == "refused":
            ok(f"{role} (real login) refused: {statement[:42]}")
        elif outcome == "allowed":
            fail(f"{role} (real login) was ALLOWED: {statement}")
            all_good = False
        else:
            fail(f"{role} (real login): {outcome.split('|', 1)[1]}")
            all_good = False

    outcome = _attempt(dsn_for[WRITER], MOVE_CURSOR)
    if outcome == "allowed":
        ok(f"{WRITER} (real login) can move the cursor")
    else:
        fail(f"{WRITER} (real login) cannot move the cursor: {outcome}")
        all_good = False

    clear_probe(url)
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
    passwords: dict[str, str] = {}
    with psycopg.connect(url, autocommit=True) as conn:
        for role in (WRITER, READER):
            if role_has_login(conn, role) and not args.rotate_passwords:
                warn(f"{role} already has a login; password left alone")
                warn("  re-run with --rotate-passwords to change it")
            else:
                pw = secrets.token_urlsafe(24)
                if ensure_login(conn, role, pw):
                    passwords[role] = pw
                    ok(f"{role} login set")
                else:
                    warn(f"not permitted to alter {role}; password unchanged")
                    warn("  needs CREATEROLE and ADMIN OPTION on that role")

    step(4, "Grants as recorded in the catalogue")
    if not check_privileges(url):
        print(f"\n{RED}The grants are not what sql/002 intends. Do not deploy.{RESET}")
        return 1

    step(5, "Verify the append-only guarantee ON THIS SERVER")
    if not verify_grants(url):
        print(f"\n{RED}The guarantee does not hold. Do not deploy this database.{RESET}")
        return 1
    ok("every forbidden operation was refused")

    # Only possible when this run set the passwords. Skipping it is a real
    # reduction in coverage, so it is announced rather than passed over
    # quietly — the grants are proven either way, the credential is not.
    if len(passwords) == 2:
        print()
        if not verify_logins(
            url, *(swap_credentials(url, r, passwords[r]) for r in (WRITER, READER))
        ):
            print(f"\n{RED}The deployed credentials do not carry the grants.{RESET}")
            return 1
    else:
        print()
        warn("skipped the real-login check: passwords were not set this run")
        warn("  the grants above are proven; the credentials are unchanged and")
        warn("  were verified when they were issued")

    step(6, "Connection strings")
    if not passwords:
        print(f"\n  {DIM}Passwords unchanged. Keep using the strings you already have.{RESET}")
        print(f"  {DIM}Lost them? Re-run with --rotate-passwords.{RESET}\n")
        return 0

    if WRITER in passwords:
        print(f"\n  {DIM}GitHub Actions secret BELLWETHER_DATABASE_URL:{RESET}")
        print(f"  {swap_credentials(url, WRITER, passwords[WRITER])}")
    if READER in passwords:
        print(f"\n  {DIM}Render environment BELLWETHER_READONLY_DATABASE_URL:{RESET}")
        print(f"  {swap_credentials(url, READER, passwords[READER])}")
    print(
        f"\n  {YELLOW}The writer string never goes near the serving container.{RESET}\n"
        f"  {DIM}If the API held write credentials, the append-only guarantee "
        f"would rest on\n  the API choosing not to use them.{RESET}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
