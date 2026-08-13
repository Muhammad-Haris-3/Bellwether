"""HTTP client for the MediaWiki Action API.

Pipeline-only. The serving container never imports this module, and CI asserts
that httpx is absent from the serving requirements.

Wikimedia is a free, public, donation-funded service. Being a polite client is
not optional courtesy — an impolite one gets the project blocked, and there is
no paid tier to fall back to.

Two rules are enforced here rather than left to callers:

  * every request carries the contact-bearing User-Agent that qualifies for the
    200 req/min tier (without it the limit is 10/min, which will not run this
    project);
  * requests are spaced to stay under a self-imposed ceiling well below that
    allowance.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bellwether.config import get_settings

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class UpstreamError(RuntimeError):
    """A non-retryable upstream failure."""


class RateLimiter:
    """Minimum-interval limiter.

    Process-local, which is sufficient because each scheduled job is a single
    process and the advisory lock in :mod:`bellwether.db` prevents two of them
    running at once. A distributed limiter would be more correct and would be
    solving a problem this architecture does not have.
    """

    def __init__(self, per_minute: int) -> None:
        self.min_interval = 60.0 / max(per_minute, 1)
        self._last: float | None = None

    def wait(self) -> None:
        now = time.monotonic()
        if self._last is not None:
            elapsed = now - self._last
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


class MediaWikiClient:
    """A single HTTP session against one wiki's api.php."""

    def __init__(
        self,
        endpoint: str = "https://en.wikipedia.org/w/api.php",
        *,
        per_minute: int | None = None,
    ) -> None:
        settings = get_settings()
        self.endpoint = endpoint
        self.limiter = RateLimiter(per_minute or settings.max_requests_per_minute)
        self.calls = 0
        self._client = httpx.Client(
            timeout=DEFAULT_TIMEOUT,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )

    def __enter__(self) -> MediaWikiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def get(self, params: dict[str, Any]) -> dict[str, Any]:
        """Issue one Action API query and return the parsed body.

        4xx other than 429 are not retried: a malformed request stays malformed
        however many times it is sent, and retrying it only wastes someone
        else's bandwidth.
        """
        self.limiter.wait()
        self.calls += 1

        response = self._client.get(self.endpoint, params={**params, "format": "json"})

        if response.status_code == 429:
            # Honour the server's own instruction if it gave one, rather than
            # substituting our backoff for their explicit answer.
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                time.sleep(min(int(retry_after), 120))
            response.raise_for_status()  # retryable

        if response.status_code >= 500:
            response.raise_for_status()  # retryable

        if response.status_code >= 400:
            raise UpstreamError(
                f"{response.status_code} from {self.endpoint}: {response.text[:300]}"
            )

        body: dict[str, Any] = response.json()

        # The Action API answers malformed queries with HTTP 200 and an error
        # object. Treating that as success is how a job ends up reporting a
        # clean run having written nothing at all.
        if "error" in body:
            err = body["error"]
            raise UpstreamError(f"API error {err.get('code')}: {err.get('info')}")

        return body
