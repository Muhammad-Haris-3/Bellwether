"""The sampling frame (M1 §3, M1-FR-1 to FR-3, FR-6).

Separated into its own module because the frame is not an implementation
detail. It is part of every downstream estimate: once ingestion runs under it,
every rate, every model and every comparison is conditional on these numbers.
Changing them later does not adjust the estimates — it invalidates comparisons
across the change.

**Why sample at all.** Neon Free is 0.5 GB. NFR-4 caps usage at 80% of that,
and a measured 372 bytes per event row makes the SRS's original frame — 100% of
logged-out edits, 120-day retention — arithmetically impossible. M1 §2.1 has
the working. Sampling is preferable to the alternatives because a documented
probability sample can be weighted back to the population, and evidence that
was never kept cannot be recovered.

**Why a hash and not a random draw.** The frame must be reproducible. Re-running
ingestion over the same window has to select the identical set, or the sample is
whatever the scheduler happened to do that day, and no one can audit it.
"""

from __future__ import annotations

import hashlib
from typing import Any

# Kept in the population at these rates. Measured volumes (M0): roughly 14,000
# logged-out and 76,000 registered main-namespace non-bot edits per day.
#
#   14,000 x 0.50  =  7,000
#   76,000 x 0.03  =  2,280
#                     -----
#                     9,280 events/day, inside the 400 MB budget (M1 §2.2)
#
# Yield at the M0 base rates: 7,000 x 22.25% + 2,280 x 3.26% = ~1,630 matured
# positives a day, so PREREGISTRATION P-3's 2,500 arrives in under two days and
# never becomes the binding constraint on a promotion. P-4's seven-day shadow
# minimum does, which is the intended order.
SAMPLE_PERCENT = {"logged_out": 50, "registered": 3}

# The share of sampled events that receive the full five-checkpoint grid rather
# than a single check at maturity (M1 §5). The grid estimates one curve in M2;
# it does not need to run on every event forever.
MATURITY_COHORT_PERCENT = 10

# Distinct salts, so the cohort decision is independent of the sampling
# decision (M1-FR-6). Sharing a salt would make the cohort a deterministic
# function of inclusion, and the survival curve would then be estimated on a
# non-random slice of the sample rather than on a random one.
_SAMPLE_SALT = "bellwether/sample/v1"
_COHORT_SALT = "bellwether/cohort/v1"


def bucket(revid: int, salt: str) -> int:
    """Map a revision id to 0–99, deterministically and stably.

    blake2b rather than Python's hash(): the built-in is randomised per process
    unless PYTHONHASHSEED is pinned, which would make the frame differ between
    runs — the one thing it must never do.
    """
    digest = hashlib.blake2b(f"{salt}:{revid}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") % 100


def is_logged_out(event: dict[str, Any]) -> bool:
    """Logged out covers IP edits and temporary accounts.

    English Wikipedia masks IPs behind temporary accounts, so `anon` is never
    set there (M0 §3). Both are kept because `anon` still occurs on other wikis
    and in historical data.
    """
    return bool(event.get("is_anon") or event.get("is_temp"))


def stratum_of(event: dict[str, Any]) -> str:
    return "logged_out" if is_logged_out(event) else "registered"


def in_sample(event: dict[str, Any]) -> bool:
    stratum = stratum_of(event)
    return bucket(int(event["revid"]), _SAMPLE_SALT) < SAMPLE_PERCENT[stratum]


def weight_of(event: dict[str, Any]) -> float:
    """Inverse sampling probability (M1-FR-2).

    A logged-out edit that survives a 50% frame stands for two; a registered
    edit surviving a 3% frame stands for 33.3. Recorded per row at observation
    time, so population estimates stay recoverable even across a change of
    frame — M0's census rows carry a weight of 1.0 and remain correct.
    """
    return 100.0 / SAMPLE_PERCENT[stratum_of(event)]


def in_maturity_cohort(event: dict[str, Any]) -> bool:
    return bucket(int(event["revid"]), _COHORT_SALT) < MATURITY_COHORT_PERCENT
