"""Issue an account (SRS FR-38 as amended 2026-08-14, M6-FR-39, M6-FR-40).

There is no sign-up page. Accounts exist because an administrator ran this, and
the password is displayed once and never recoverable — the same pattern
`bootstrap_database.py` already uses for database roles, for the same reason:
a secret that can be re-read is a secret stored somewhere.

Run with the OWNER connection string, not the application's. Creating a user is
not something the running application is allowed to do, and this script is
deliberately outside it.

    python scripts/create_user.py "postgresql://owner:pw@host/db" alice@example.com reviewer

Roles:

    viewer     read the queue and the public pages
    reviewer   the above, plus record judgements
    admin      the above, plus read the audit log and freeze automation
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bellwether import auth  # noqa: E402

GREEN, YELLOW, RED, BOLD, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"

ROLES = ("viewer", "reviewer", "admin")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dsn", help="owner connection string")
    parser.add_argument("email")
    parser.add_argument("role", choices=ROLES)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="issue a new password for an existing account, revoking its sessions",
    )
    args = parser.parse_args()

    email = args.email.strip().lower()
    if "@" not in email or len(email) < 3:
        print(f"{RED}{email!r} does not look like an address.{RESET}")
        return 1

    password = auth.generate_password()
    digest, salt, params = auth.hash_password(password)

    with psycopg.connect(args.dsn, row_factory=dict_row) as conn:
        existing = conn.execute(
            "SELECT user_id, role FROM app.users WHERE email = %s", (email,)
        ).fetchone()

        if existing and not args.reset:
            print(f"{RED}{email} already exists.{RESET} Use --reset to issue a new password.")
            return 1

        if existing:
            # A reset revokes every live session. Otherwise the old password is
            # replaced while whoever is holding a stolen cookie stays signed in,
            # which is the situation a reset is usually a response to.
            conn.execute(
                "UPDATE app.users SET password_hash = %s, password_salt = %s, kdf_params = %s "
                "WHERE user_id = %s",
                (digest, salt, Json(params), existing["user_id"]),
            )
            revoked = conn.execute(
                "UPDATE app.sessions SET revoked_at = now() "
                "WHERE user_id = %s AND revoked_at IS NULL",
                (existing["user_id"],),
            ).rowcount
            user_id = existing["user_id"]
            action = "reset_password"
        else:
            user_id = conn.execute(
                "INSERT INTO app.users (email, password_hash, password_salt, kdf_params, role) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING user_id",
                (email, digest, salt, Json(params), args.role),
            ).fetchone()["user_id"]
            revoked = 0
            action = "create_user"

        # M6-FR-40. Issuing an account is itself an audited admin action, even
        # though it happens outside the application — a log that only records
        # what the app did would show accounts appearing from nowhere.
        conn.execute(
            "INSERT INTO app.audit_log (actor, actor_role, action, target, outcome, detail) "
            "VALUES (NULL, 'admin_cli', %s, %s, 'allowed', %s)",
            (action, email, f"role={args.role}"),
        )

    print()
    print(f"{BOLD}{'Account issued' if action == 'create_user' else 'Password reset'}{RESET}")
    print(f"  email    {email}")
    print(f"  role     {args.role}")
    print(f"  user id  {user_id}")
    if revoked:
        print(f"  {YELLOW}revoked {revoked} live session(s){RESET}")
    print()
    print(f"  {BOLD}password {GREEN}{password}{RESET}")
    print()
    print(f"  {YELLOW}Shown once. It is stored only as a scrypt hash and cannot be")
    print(f"  recovered — a lost password means running this again with --reset.{RESET}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
