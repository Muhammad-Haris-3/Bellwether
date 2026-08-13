"""Configuration, read from the environment.

Two database URLs, deliberately. The pipeline writes; the API reads. If they
resolve to the same role then the API can write to tables the project's claims
rest on, and the append-only guarantee is decoration — so
:meth:`Settings.assert_roles_distinct` says so out loud rather than letting it
pass.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# An environment label is a short word. Anything else — most importantly a
# connection string pasted into the wrong variable — must never reach a
# response body.
ENV_LABEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,19}$")

# The checkpoints at which a previously ingested edit is re-checked for the
# mw-reverted tag (M0-T4).
#
# These are not a guess at the maturity window. They are the sampling grid from
# which M2 estimates it: a Kaplan-Meier curve needs observations spread across
# the range where the event of interest actually happens, and reverts are
# heavily front-loaded, hence the log-ish spacing. The 7-day point exists to
# bound the tail, not because anything is expected to be learned at day six.
LABEL_CHECKPOINTS_SECONDS: tuple[int, ...] = (
    3_600,  # 1 hour
    21_600,  # 6 hours
    86_400,  # 24 hours
    172_800,  # 48 hours
    604_800,  # 7 days
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BELLWETHER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = ""
    readonly_database_url: str = ""
    env: str = "local"

    # Left empty so the hosting platform's own build identifier can fill it in;
    # see build_id. Defaulting to "local" would mean a production deployment
    # quietly reporting itself as local.
    commit: str = ""

    # Wikimedia's User-Agent policy: an unauthenticated client sending a
    # meaningful UA with contact details gets 200 req/min. Without one it gets
    # 10 req/min, which is not enough to run this project. The contact address
    # is a requirement of the policy, not politeness.
    contact_email: str = "hariskhokhar975@gmail.com"
    repo_url: str = "https://github.com/Muhammad-Haris-3/Bellwether"

    # Self-imposed ceiling, well under the 200/min allowance (NFR-2).
    max_requests_per_minute: int = 40

    # How far back a first-ever run reaches when no cursor exists. Short on
    # purpose: a cold start should begin producing rows immediately and let
    # gap-filling extend the history, rather than spend its first run on a
    # backfill that may time out and leave no cursor at all.
    cold_start_lookback_minutes: int = 60

    # Bounds one ingestion run so it finishes inside the workflow's 10-minute
    # budget (NFR-5). A run that hits the cap is not an error: it advances the
    # cursor as far as it got and the next run continues.
    max_pages_per_run: int = 40

    @field_validator("env")
    @classmethod
    def _reject_anything_that_is_not_a_label(cls, value: str) -> str:
        """Never echo an unexpected value in the env field.

        Health and status endpoints are public and unauthenticated, and they
        report `env` verbatim. A connection string pasted into the wrong
        environment variable would therefore be published to the internet with
        its password intact. That happened once on a previous project; this
        validator is why it cannot happen here.

        Anything that is not a short lowercase label becomes "misconfigured".
        Refusing to boot would be worse — it would take the service down over a
        cosmetic field and remove the very status page that explains why.
        """
        value = value.strip()
        return value if ENV_LABEL.match(value) else "misconfigured"

    @property
    def env_is_valid(self) -> bool:
        return self.env != "misconfigured"

    @property
    def user_agent(self) -> str:
        """The User-Agent every outbound request carries.

        Format follows the example in Wikimedia's policy: name/version, then
        a URL and an email in parentheses.
        """
        from bellwether import __version__

        return f"Bellwether/{__version__} ({self.repo_url}; {self.contact_email})"

    @property
    def build_id(self) -> str:
        """The commit this instance is running, whoever deployed it."""
        return (
            self.commit
            or os.environ.get("RENDER_GIT_COMMIT")
            or os.environ.get("VERCEL_GIT_COMMIT_SHA")
            or os.environ.get("GITHUB_SHA")
            or "local"
        )

    @property
    def serving_url(self) -> str:
        """The URL the API should read from, falling back to the writer URL.

        The fallback is convenient locally and dangerous in production, which
        is why :attr:`readonly_role_in_use` is reported by the status endpoint
        rather than assumed.
        """
        return self.readonly_database_url or self.database_url

    @property
    def serving_host(self) -> str:
        """The host actually being read from, credentials stripped.

        An endpoint answering from the wrong database looks exactly like an
        endpoint answering.
        """
        url = self.serving_url
        if not url:
            return "NOT CONFIGURED"
        return url.split("@")[-1].split("?")[0]

    @property
    def readonly_role_in_use(self) -> bool:
        return bool(self.readonly_database_url) and (
            self.readonly_database_url != self.database_url
        )

    def assert_roles_distinct(self) -> None:
        if self.readonly_database_url and self.readonly_database_url == self.database_url:
            raise ValueError(
                "BELLWETHER_READONLY_DATABASE_URL is identical to "
                "BELLWETHER_DATABASE_URL. The serving role must be a distinct "
                "read-only role, or the API can write to the evidence tables."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
