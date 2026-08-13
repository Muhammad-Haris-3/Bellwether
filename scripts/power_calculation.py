"""Minimum sample size for a promotion decision (M0-T9).

The pre-registration fixes how many matured, labelled events a challenger must
accumulate before it may be promoted. That number is not a guess: it is the
sample at which the pre-registered test has an 80% chance of detecting the
pre-registered margin, given the base rate M0 measured.

PR-AUC has no usable closed-form standard error, so the sampling distribution
is simulated. The chain is:

    1. Simulate paired champion/challenger scores at a known base rate, with a
       known PR-AUC difference.
    2. Confirm the paired bootstrap's standard error matches the true sampling
       spread — otherwise the test statistic is not what the power calculation
       assumes it is.
    3. Solve for the N at which that spread is small enough for 80% power.
    4. Verify by simulation at the answer.

Run:  python scripts/power_calculation.py

Deterministic: seeded, so the numbers in PREREGISTRATION.md can be reproduced
exactly. It is committed for that reason — a pre-registered sample size that
cannot be recomputed is an assertion, not a commitment.
"""

from __future__ import annotations

from math import erf

import numpy as np
from sklearn.metrics import average_precision_score

SEED = 20260813
ALPHA = 0.05
POWER = 0.80

# Measured on 20,000 matured (48h+) edits, 2026-08-13. See SRS 6.3 / VER-3.
RATE_LOGGED_OUT = 0.2225
RATE_REGISTERED = 0.0326
SHARE_LOGGED_OUT = 0.157

# Candidate registered-stratum sampling rate. M1 sets the real value from
# measured volume; the pre-registered minimum is expressed in POSITIVES rather
# than total events precisely so that choice cannot move it afterwards.
R = 0.20

# Two-sided normal quantiles for the target error rates.
Z_ALPHA = 1.959964
Z_POWER = 0.841621


def evaluation_set_positive_rate() -> float:
    """Positive rate among sampled edits, under the case-control frame."""
    kept_logged_out = SHARE_LOGGED_OUT
    kept_registered = (1 - SHARE_LOGGED_OUT) * R
    positives = kept_logged_out * RATE_LOGGED_OUT + kept_registered * RATE_REGISTERED
    return positives / (kept_logged_out + kept_registered)


# How alike two successive models are, on the latent scale.
#
# This is the single most consequential assumption in the calculation, because
# a paired test's power is driven by the variance of the DIFFERENCE, and that
# shrinks as the models converge. Setting it too high makes the required sample
# look far smaller than it is.
#
# The first version of this script modelled the challenger as the champion plus
# a deterministic term, which is effectively rho = 1. It reported a required
# sample of 169 events; the verification step measured the real power at that
# sample as 0.36. That failure is why the verification step exists, and why the
# assumption is now named, exposed, and deliberately conservative.
#
# 0.85 stands for two gradient-boosted models trained on the same features over
# windows a week apart: strongly related, not interchangeable.
RHO = 0.85


def simulate_pair(
    rng: np.random.Generator, n: int, p: float, separation: float, lift: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One paired sample: labels, champion scores, challenger scores.

    Both models score the same events — that is what makes the comparison
    paired, and paired is what makes a small difference detectable at a sample
    size this project can actually reach.
    """
    y = rng.binomial(1, p, n)
    signal = separation * y
    champion = signal + rng.normal(0, 1.0, n)

    # A genuinely different model, correlated with the champion rather than
    # derived from it: shared latent component plus its own independent one.
    shared = RHO * (champion - signal)
    own = np.sqrt(1 - RHO**2) * rng.normal(0, 1.0, n)
    challenger = signal + shared + own + lift * y
    return y, champion, challenger


def pr_auc_difference(y: np.ndarray, champion: np.ndarray, challenger: np.ndarray) -> float:
    if y.sum() == 0:
        return np.nan
    return float(average_precision_score(y, challenger) - average_precision_score(y, champion))


def calibrate(rng: np.random.Generator, p: float, target_pr_auc: float) -> float:
    """Find the separation giving a champion of roughly the assumed quality."""
    lo, hi = 0.0, 4.0
    for _ in range(30):
        mid = (lo + hi) / 2
        y, champ, _ = simulate_pair(rng, 200_000, p, mid, 0.0)
        if average_precision_score(y, champ) < target_pr_auc:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def calibrate_lift(
    rng: np.random.Generator, p: float, separation: float, target_delta: float
) -> float:
    lo, hi = 0.0, 3.0
    for _ in range(30):
        mid = (lo + hi) / 2
        y, champ, chal = simulate_pair(rng, 200_000, p, separation, mid)
        if pr_auc_difference(y, champ, chal) < target_delta:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def sampling_sd(
    rng: np.random.Generator, n: int, p: float, separation: float, lift: float, reps: int
) -> float:
    diffs = [pr_auc_difference(*simulate_pair(rng, n, p, separation, lift)) for _ in range(reps)]
    return float(np.nanstd(diffs, ddof=1))


def normal_power(delta: float, sd: float) -> float:
    """Power of a two-sided test at ALPHA, given the effect and its spread."""
    return float(1 - 0.5 * (1 + erf((Z_ALPHA - delta / sd) / np.sqrt(2))))


def bootstrap_se(
    rng: np.random.Generator,
    y: np.ndarray,
    champion: np.ndarray,
    challenger: np.ndarray,
    resamples: int,
) -> float:
    """Paired bootstrap: resample EVENTS, keeping both models' scores together.

    Resampling the two models independently would destroy the pairing and
    inflate the standard error, making the test far too conservative — a
    challenger that genuinely won would never be promoted, and the failure
    would look like the challenger's fault.
    """
    n = len(y)
    out = np.empty(resamples)
    for i in range(resamples):
        idx = rng.integers(0, n, n)
        out[i] = pr_auc_difference(y[idx], champion[idx], challenger[idx])
    return float(np.nanstd(out, ddof=1))


def main() -> None:
    rng = np.random.default_rng(SEED)

    p = evaluation_set_positive_rate()
    print(f"Positive rate in the sampled evaluation set : {p:.4f}")
    print(
        f"  from measured {RATE_LOGGED_OUT:.4f} logged-out / "
        f"{RATE_REGISTERED:.4f} registered at R={R}"
    )

    target_pr_auc = 0.35
    target_delta = 0.02

    separation = calibrate(rng, p, target_pr_auc)
    lift = calibrate_lift(rng, p, separation, target_delta)
    y, champ, chal = simulate_pair(rng, 400_000, p, separation, lift)
    print(
        f"\nAssumed champion PR-AUC                     : {average_precision_score(y, champ):.4f}"
    )
    print(f"Assumed challenger PR-AUC                   : {average_precision_score(y, chal):.4f}")
    print(f"Pre-registered margin (delta)               : {target_delta:.4f}")

    # Step 2 — does the bootstrap SE match the true sampling spread?
    n_ref = 20_000
    true_sd = sampling_sd(rng, n_ref, p, separation, lift, reps=200)
    y_r, c_r, h_r = simulate_pair(rng, n_ref, p, separation, lift)
    boot_se = bootstrap_se(rng, y_r, c_r, h_r, resamples=1_000)
    print(f"\nAt N={n_ref:,}")
    print(f"  true sampling SD of the difference        : {true_sd:.5f}")
    print(f"  paired bootstrap SE (one sample)          : {boot_se:.5f}")
    print(f"  ratio                                     : {boot_se / true_sd:.3f}")

    # Step 3 — search N directly. Do NOT extrapolate by 1/sqrt(N).
    #
    # That rule is asymptotic, and PR-AUC's sampling distribution is skewed and
    # heavy-tailed when the positive count is small. Extrapolating from
    # N=20,000 down to a few hundred events understated the standard deviation
    # by a factor of two, and the power by more. Simulating at each candidate N
    # costs a few minutes and cannot make that mistake.
    print(f"\n{'N (events)':>12}{'positives':>11}{'SD of diff':>13}{'power':>9}")
    chosen: int | None = None
    for n in (2_000, 5_000, 10_000, 20_000, 50_000, 100_000, 200_000):
        sd = sampling_sd(rng, n, p, separation, lift, reps=150)
        power = normal_power(target_delta, sd)
        print(f"{n:>12,}{int(n * p):>11,}{sd:>13.5f}{power:>9.3f}")
        if chosen is None and power >= POWER:
            chosen = n

    if chosen is None:
        print("\nNo N on the grid reaches the target power. Widen the margin or the grid.")
        return

    print(f"\nSmallest N on the grid reaching {POWER:.0%} power : {chosen:,} events")
    print(f"Equivalent matured POSITIVES                : {int(np.ceil(chosen * p)):,}")

    # Step 4 — verify with the actual test rather than the normal
    # approximation. Each replicate runs the paired bootstrap and asks whether
    # the interval excludes zero, which IS the pre-registered decision rule.
    hits = 0
    trials = 60
    for _ in range(trials):
        y_t, c_t, h_t = simulate_pair(rng, chosen, p, separation, lift)
        diff = pr_auc_difference(y_t, c_t, h_t)
        se = bootstrap_se(rng, y_t, c_t, h_t, resamples=400)
        if abs(diff) - Z_ALPHA * se > 0:
            hits += 1
    print(f"\nVerification by the pre-registered test at N={chosen:,}")
    print(f"  bootstrap-based power over {trials} trials   : {hits / trials:.3f}")


if __name__ == "__main__":
    main()
