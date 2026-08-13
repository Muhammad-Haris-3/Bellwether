"""Diagnose a connection string without printing it.

When the deployed API reports `database_reachable: false`, /health gives only
the exception class — deliberately, because psycopg's connection errors quote
the host, the user, and sometimes the whole connection string, and /health is
public. That safety costs diagnosability, so this script pays it back: run it
on your own machine, where the full error is not a publication.

Everything it prints is safe to share. The password is masked wherever it
appears, including inside the driver's error message.

Usage — run it with NO arguments and paste the string at the prompt:

    python scripts/check_connection.py

The prompt does not echo, so the string never reaches your shell history, your
terminal scrollback, or a screenshot. Passing it as an argument still works and
is still accepted, because a script that refuses the obvious invocation gets
worked around rather than used.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from urllib.parse import urlsplit

import psycopg

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def mask(text: str, secret: str | None) -> str:
    if secret:
        text = text.replace(secret, "********")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "url",
        nargs="?",
        help="Connection string. Omit it to be prompted without echo, which is safer.",
    )
    args = parser.parse_args()

    url = args.url or getpass.getpass("Connection string (input hidden): ").strip()
    if not url:
        print("No connection string given.")
        return 2

    parts = urlsplit(url)
    password = parts.password
    host = parts.hostname or ""

    print("\nWhat this string says")
    print(f"  user      {parts.username}")
    print(f"  host      {host}")
    print(f"  database  {(parts.path or '').lstrip('/')}")
    print(f"  options   {parts.query or '(none)'}")
    print(f"  password  {'set' if password else 'MISSING'}")

    print("\nSanity checks")
    pooled = "-pooler" in host
    if parts.username == "bellwether_readonly":
        print(f"  {GREEN}OK{RESET}   read-only role, correct for the API")
        if pooled:
            print(f"  {GREEN}OK{RESET}   pooled endpoint, correct for the API")
        else:
            print(f"  {YELLOW}WARN{RESET} not the pooled endpoint; Render should use -pooler")
    elif parts.username == "bellwether_writer":
        print(f"  {GREEN}OK{RESET}   writer role, correct for GitHub Actions")
        if pooled:
            print(f"  {YELLOW}WARN{RESET} pooled endpoint; the pipeline should use the direct one")
    else:
        print(f"  {YELLOW}WARN{RESET} role is '{parts.username}', not one of the two app roles")

    # The failure this catches actually happened: the owner's password was
    # pasted alongside an app role's username, and the only symptom was
    # "password authentication failed" — which reads as a wrong password rather
    # than as the wrong password.
    #
    # Neon issues owner passwords with an npg_ prefix. Both app roles get
    # secrets.token_urlsafe(24) from the bootstrap script, which never does.
    if parts.username in {"bellwether_readonly", "bellwether_writer"} and (
        password or ""
    ).startswith("npg_"):
        print(f"  {RED}FAIL{RESET} this is the OWNER's password on an app role's username")
        print("       npg_ is Neon's own format for neondb_owner. The app roles")
        print("       get their passwords from scripts/bootstrap_database.py")
        print("       --rotate-passwords, and they never start with npg_.")

    print("\nConnecting")
    try:
        with psycopg.connect(url, connect_timeout=15) as conn:
            row = conn.execute(
                "SELECT current_user AS usr, current_database() AS db, version() AS v"
            ).fetchone()
            assert row is not None
            print(f"  {GREEN}OK{RESET}   connected as {row[0]} to {row[1]}")
            print(f"  {DIM}{row[2][:60]}{RESET}")

            counts = conn.execute(
                "SELECT (SELECT count(*) FROM landing.rc_events) AS events, "
                "       (SELECT count(*) FROM outcome.labels)    AS labels"
            ).fetchone()
            assert counts is not None
            print(f"  {GREEN}OK{RESET}   can read: {counts[0]:,} events, {counts[1]:,} labels")
    except Exception as exc:  # noqa: BLE001
        print(f"  {RED}FAIL{RESET} {type(exc).__name__}")
        for line in mask(str(exc), password).splitlines():
            if line.strip():
                print(f"       {line.strip()[:160]}")
        print(f"\n{DIM}The password above is masked. This output is safe to share.{RESET}")
        return 1

    print(f"\n{GREEN}This string works.{RESET} If the deployed API still reports")
    print("database_reachable: false, the value in the dashboard is not this one.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
