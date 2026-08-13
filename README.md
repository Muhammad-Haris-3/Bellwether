# Bellwether

**A model that notices it is getting worse, and replaces itself.**

Bellwether scores English Wikipedia edits for the probability they will be
reverted, publishes each score **before the outcome exists**, and grades itself
automatically when the outcome arrives. When its own accuracy decays, it
retrains, tests the replacement against live traffic in shadow mode, and
promotes or rejects it under a rule committed to this repository **before any
model existed** — then writes down why.

The claim is not "my model is accurate." It is: *here is a system that
maintained itself, and here is the evidence it did so honestly.*

**Live:** [bellwether-phi.vercel.app](https://bellwether-phi.vercel.app) ·
**API:** [bellwether-fyyz.onrender.com/health](https://bellwether-fyyz.onrender.com/health)

> **Status: M0–M2 complete.** The label loop is proven, ingestion runs
> unattended within a fixed storage budget, and **KC-2 is cleared** — there
> is real signal beyond the trivial heuristic, measured against a margin
> fixed before any model existed. The verdict, its null check and its
> ablation are public at [/kc2](https://bellwether-fyyz.onrender.com/kc2).
>
> The maturity window is still unset, and
> [/maturity](https://bellwether-fyyz.onrender.com/maturity) says so in its
> own response rather than serving a number.

---

## Why this is hard to fake

| Mechanism | What it prevents |
|---|---|
| **Append-only by grant.** The pipeline role holds no `UPDATE` or `DELETE` on the outcome tables — enforced by a PostgreSQL grant, verified on the production server by `scripts/bootstrap_database.py`, not by code convention | Retrospective editing of what was known, and when |
| **Pre-registration**, committed before the first model is trained | Choosing the promotion rule after seeing which model won |
| **Label maturity.** No prediction enters a published metric until its outcome has had time to arrive, with the window estimated by survival analysis rather than guessed | Optimistic bias from counting a not-yet-reverted edit as a negative |
| **Point-in-time features**, computed only from events already ingested, with a guard that raises on violation | Lookahead leakage — the failure that silently invalidates every accuracy claim |
| **Negative observations retained.** The label checks that found nothing are kept, at the age they found nothing | A survival curve estimated only from the positives, which is not a survival curve |
| **Two independent label paths**, recorded separately and never silently reconciled | A single upstream quirk becoming ground truth |

## Architecture

```
MediaWiki Action API (recentchanges, revisions)
      │  keyless · 200 req/min with a contact-bearing User-Agent
      ▼
GitHub Actions — ingest · label · features · metrics · drift · train · decide
      │  cursor-based · idempotent · gap-filling · run-logged · advisory-locked
      ▼
landing   rc_events        insert-only, sampled, weighted at observation time
      ▼
outcome   labels · label_checks       matured only, first observation wins
      ▼
serve     FastAPI (read-only role) → Next.js
```

The pipeline runs on GitHub Actions rather than a long-lived process because
the project has a hard zero-cost constraint. The consequence — near-real-time
rather than streaming, edits scored within one polling interval — is recorded
in [the SRS §3.2](Bellwether_SRS_v1.0.md) rather than glossed over.

## What M0 has established

Measured against the live API on 2026-08-13:

| Finding | Value |
|---|---|
| `mw-reverted` retrievable for previously ingested edits | **Yes** — the loop closes. KC-1 retired |
| Revert rate, logged-out editors | **21.2%** (n=538, 6–24h maturity, a lower bound) |
| Revert rate, registered editors | **1.6%** (n=4,462) |
| Main-namespace non-bot edit volume | ~62/minute, ≈ 90k/day |
| Logged-out editors identifiable by `anon` | **No** — English Wikipedia uses temporary accounts; `anon` was set on 0 of 2,498 edits |
| Secondary label path, precision | **100%** — 6 of 6 agreed with the primary path |
| Secondary label path, recall | **~19%** — 6 of 32. Conservative by design; the gap is measured, not assumed |

Two of those rows changed the design rather than confirming it. The temporary
accounts finding rewrote the sampling frame — a frame keyed on `anon` would
have sampled away most of the positive class before training began. And undo
summaries turned out to name their target as `[[Special:Diff/N|N]]` rather than
a bare number, so the first parser derived nothing from 97 of 97 undos while
reporting a clean run.

Finding those in week one is what M0 is for.

## Documents

| | |
|---|---|
| [Bellwether_SRS_v1.0.md](Bellwether_SRS_v1.0.md) | Requirements, feasibility, risks, acceptance criteria |
| [Bellwether_M0_Spec.md](Bellwether_M0_Spec.md) | The milestone spec, its kill criterion, and what it must not become |
| [Bellwether_M0_Summary.md](Bellwether_M0_Summary.md) | What M0 cost: four findings that changed the design, and ten bugs |
| [Bellwether_M1_Spec.md](Bellwether_M1_Spec.md) | The storage budget, the sampling frame, and a requirement withdrawn rather than built |
| [Bellwether_M1_Summary.md](Bellwether_M1_Summary.md) | What M1 measured, including two contradictory clauses in the SRS |
| [Bellwether_M2_Spec.md](Bellwether_M2_Spec.md) | The maturity window, the leakage traps, and the criterion that can end the project |
| [Bellwether_M2_Summary.md](Bellwether_M2_Summary.md) | KC-2 cleared — and the ablation showing it rests on one drifting feature |
| [Bellwether_M3_Spec.md](Bellwether_M3_Spec.md) | The prediction register, and fixing the drifting feature before deploying it |
| [PREREGISTRATION.md](PREREGISTRATION.md) | The promotion rule, fixed before any model existed |

## Local setup

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
```

```bash
python scripts/bootstrap_database.py "postgresql://owner:pw@host/db"
```

The bootstrap script applies the schema, sets up both roles, and then **tries
every forbidden operation and requires the database to refuse it**. A grant
that was supposed to be applied and silently was not looks exactly like one
that was, until the moment it matters.

```bash
BELLWETHER_DATABASE_URL=... python -m bellwether.ingest --max-pages 3
```
