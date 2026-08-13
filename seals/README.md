# Seals

One file per completed month, written by `python -m bellwether.seal` and
committed by the **Maintain** workflow.

Each file records a SHA-256 digest over that month's evidence rows, read in a
fixed order, together with the row counts that went into it.

## Why they exist

The SRS promised that evidence — labels, and predictions from M3 — would be
kept indefinitely, because the project's claims rest on it. Measured against
Neon Free's 0.5 GB that promise cannot be kept.

Rather than break it quietly, the rows are **sealed before they are pruned**.
The database ages them out; the proof that they were not altered stays here, in
public git history, verifiable by someone with access to neither the database
nor the author.

## What a seal proves

- **It proves** the rows for that month have not changed since sealing.
  Recompute the digest and compare.
- **It does not prove** the rows were correct when written. Nothing here does.
  That is what the append-only grants and `PREREGISTRATION.md` are for.
- **It cannot distinguish** deliberate tampering from legitimate pruning: both
  leave fewer rows than were sealed. This is why verification runs *before*
  each new seal, and why row counts are published alongside the digest instead
  of being reduced to a pass/fail.

## Verifying one yourself

```bash
python -m bellwether.seal --verify --month 2026-08
```

Reports the committed digest, the recomputed digest, and both row counts.
