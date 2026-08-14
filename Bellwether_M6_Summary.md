# Bellwether — M6 Summary

**The application.**
Written 2026-08-14. 410 tests. 39 of 40 requirements built; **10 of 11
acceptance criteria satisfied, 1 pending formal verification**.

M6 is the first milestone where the risk is not primarily about fooling myself
with my own data. Every milestone before it built habit around leak guards,
maturity windows, and pre-registered thresholds. M6's risks are different in
kind — a session that can be stolen, a role enforced in the wrong layer, a queue
that shows a reviewer an unmatured item and lets them believe it is a result.
That shift in risk is worth naming, because the habits that made M0–M5 careful
do not transfer automatically. A leak guard does not stop a session fixation bug.

---

## 1. Status

| | |
|---|---|
| Requirements built | 39 of 40 |
| Acceptance criteria met | 10 of 11 |
| **Audit log seal manifest** | **not yet — M6-FR-37 deferred to M7** |
| Keyboard/contrast verification | pending — queue is keyboard-operable by construction, formal axe pass not run |

---

## 2. Findings

### 2.1 The queue showed zero items

The first working QUEUE_SQL read the serving champion from
`decide.champion_history` only. Nothing has been promoted yet, so that table is
empty. The queue returned zero rows with no error — the scorer had run, the
predictions existed, and the result was an empty list with a message saying the
scorer had not run.

`registry.champion()` has its own fallback: if the history is empty, the
champion is the newest registered model by training date. The queue needed the
same rule and did not have it.

The fix is the COALESCE in the `serving` CTE: champion history first, registry
fallback if it is empty. The comment names the duplication deliberately — this
is a second implementation of "which model is serving", and a reader finding it
without context would read it as either redundant or wrong.

### 2.2 The maturity window cap was set to exactly the maturity window

The first version capped `hours` at 168 — exactly the seven-day maturity
window. An item old enough to be matured is therefore at the far edge of the
window, and whether it appears depends on sub-second timing. A reviewer who
opened a four-week-old judgement to see how it turned out would see nothing.

The maturity marker (FR-41) exists precisely so a reviewer can see a matured
item beside an immature one and understand the difference. A cap equal to the
window makes `matured=true` a theoretical state, never an observable one.

Cap is now 720 hours (30 days). The comment in the code explains why it must
exceed the maturity window or the marker distinguishes nothing.

### 2.3 The writer's RLS trap appeared twice

`bellwether_writer` connects as a pipeline role and never sets a user session.
`app` schema tables have FORCE RLS with policies that read
`app.acting_user()` — a transaction-local setting present only during app
requests. The writer gets no rows and no error; it just sees nothing.

This appeared in two unrelated places:

**Promote checking the freeze.** `promote.py` needed to know whether automation
was frozen before doing any work. A direct `SELECT FROM app.automation_freeze`
returns nothing for the writer. Fix: `app.is_automation_frozen()`, a SECURITY
DEFINER function that runs as the owner, exposes a single boolean, and grants
EXECUTE to the writer. The writer learns one fact and cannot enumerate freeze
rows, read who set the freeze, or touch the table.

**Pruning counting sessions.** `landing.prune_expired(dry_run := true)` needed
to count expired sessions. The writer cannot SELECT from `app.sessions` for the
same reason. Fix: route the count through the existing SECURITY DEFINER
`landing.prune_expired` function itself — it runs as the owner with BYPASSRLS,
so the count is accurate and no new privilege is granted.

Both fixes follow the same pattern: a SECURITY DEFINER function as the minimum
interface between two privilege domains.

### 2.4 Authentication decision: no email, invite-only

SRS FR-38 required email-based sign-in. Every option requires either a paid
service, a personal mailbox in the loop, or a documented deviation. The
project's NFR-1 forbids paying; the other two have operational costs a small
reviewer pool does not justify.

**Decided before implementation:** accounts are issued by an administrator with
a strong generated password shown once, the same pattern `bootstrap_database.py`
already uses for database roles. No email is sent. SRS FR-38 is amended and the
reasoning is recorded there. What is given up — self-service sign-up, password
reset by email — is acceptable for a handful of reviewers and explicitly not
acceptable for a real deployment.

The password is hashed with `hashlib.scrypt` (FR-38 in the M6 spec) so the
serving image gains no new dependency.

### 2.5 Replacing a function reverted an earlier migration's extension

`landing.prune_expired` has been defined three times. `sql/005` created it,
`sql/011` **extended** it to prune `register.predictions` under the seal guard,
and `sql/025` replaced it again to add `app.sessions`.

That last one was written from 005's body. `CREATE OR REPLACE FUNCTION` silently
reverted 011's addition: predictions would never have been pruned again, and
their seal guard would have gone with them — storage growing without bound
against NFR-4, and the one mechanism ensuring evidence is attested before
deletion quietly absent.

Nothing about it fails loudly. The function still ran, still reported rows for
four tables, and simply stopped mentioning a fifth. `tests/test_register.py`
caught it because that test asserts the seal guard specifically.

The lesson is about the mechanism rather than the omission: replacing a function
that a later migration extended reverts that extension without a word, so the
replacement must be a superset of every version before it and not merely of the
one it was copied from. A new test asserts the FULL set of prune targets rather
than any single one, so the next replacement that forgets a limb fails on the
set rather than on whichever table happened to have its own test.

### 2.6 The freeze actor is not returned to the caller

The GET `/admin/freeze` endpoint does not return the `actor` field even though
it is stored. The decision was made during implementation: a freeze state that
any authenticated user can read (so a frozen system looks frozen to the people
working in it) should not become a way for a viewer to learn which admin took
a specific action. The fact of the freeze is what they need; the identity of
who set it is internal admin state. FR-29 prohibits identifying reviewers on
public pages; the same principle applies to internal pages by extension.

---

## 3. What was built

| Component | |
|---|---|
| `sql/022` | `app` schema: users, sessions, human\_labels, audit\_log, automation\_freeze; `bellwether_app` role; FORCE RLS on all five tables |
| `sql/023` | `app.credentials_for()`, `app.session_for()` — SECURITY DEFINER auth lookups callable by the app role |
| `sql/024` | Usage grants on `app` schema; DELETE on sessions for writer; RLS policy allowing session pruning |
| `sql/025` | `landing.prune_expired` extended to include `app.sessions`; reports rows deleted per target |
| `sql/026` | `app.is_automation_frozen()` — SECURITY DEFINER boolean readable by writer and app without a user session |
| `api/sessions.py` | Sign-in (scrypt verify, rate-limited), sign-out (DELETE session row), `current_user`, `require_role`, `acting_connection`, `check_csrf`, `audit()` |
| `api/review.py` | `/queue` (ranked by serving champion, matured/immature on every item), `/labels` (score read from DB, not trusted from client), `/admin/freeze` (GET any signed-in; POST admin only + CSRF) |
| `bellwether/promote.py` | Freeze check via `app.is_automation_frozen()` before any promotion work |
| `bellwether/schema.py` | Two new expectations: 025 checks function body contains `app.sessions`; 026 checks `app.is_automation_frozen` exists |
| Public timeline | `/decisions` page — every promotion, rejection and rollback with evidence, no authentication |
| Public metrics | `/metrics` page — current and historical performance, maturity window and sample size on every figure |

### 3.1 Decisions that will not be obvious later

**Score is read from the database, not trusted from the request.** `POST /labels`
reads the score from `register.predictions`, not from the judgement body. A
client that supplied its own score could file a label against a number nobody was
ever shown. The champion version and maturity state at the time of judgement are
similarly read from the database and stored with the label — by M7 those are
unrecoverable unless recorded now.

**The `automation_freeze` table is append-only by grant.** An admin can INSERT
a new row but cannot UPDATE or DELETE any past one. The current freeze state is
always the latest row. An admin who changes their mind records a new row, not a
corrected one. This is the same append-only guarantee that holds for every other
decision in the system, applied to freeze decisions for the same reason.

**`bellwether_app` writes to exactly two app tables.** INSERT on `human_labels`
and `audit_log`; SELECT on the rest. The grant is structural: no application
code discipline can accidentally give it write access to `predictions`,
`model_decisions`, or `labels`. NFR-8 is now kept by a privilege list, not a
convention.

**The proxy that makes the session cookie first-party is M6, not M7.** Without
it the cookie would be third-party on a custom domain, blocked by default by
most browsers. This was infrastructure, not a feature, and it was shipped before
the sign-in form existed.

---

## 4. Acceptance criteria

| # | | |
|---|---|---|
| D-1 | Sign-in; session server-side and hashed at rest | met — scrypt, 256-bit token, token\_hash stored |
| D-2 | Each role sees and does exactly what its policy allows | met — `test_rls.py` connects as each role and verifies |
| D-3 | **Database still refuses with app's role checks removed** | met — RLS is FORCE; `test_rls.py` demonstrates refusal without application code |
| D-4 | `bellwether_app` cannot write to any evidential table | met — by grant; writer role cannot insert to `predictions`, `model_decisions`, `labels`, `rc_events`, `evaluations`, `seals` |
| D-5 | Queue ranks by serving champion and follows a rollback | met — COALESCE over champion\_history and registry fallback |
| D-6 | Every item states matured or immature; no accuracy over queue | met — `matured` on every row; `reverted` is null until matured; no PR-AUC over unmatured items |
| D-7 | Judgement recorded with identity, confidence, champion version, score shown | met — score and champion read from DB at label time |
| D-8 | Failed authorisation appears in audit log | met — `refused` outcome written before any 404/403 raise |
| D-9 | Admin can freeze; cannot alter a past decision | met — INSERT only; freeze history is immutable |
| D-10 | Public pages load without authentication; no reviewer identified | met — `/decisions`, `/metrics`, `/performance`; actor omitted from freeze GET |
| D-11 | Queue fully keyboard-operable; passes contrast checks | **pending** — keyboard-operable by construction; formal axe pass not run |

D-3 is the criterion that matters. D-1 through D-10 can all be satisfied by
careful application code; D-3 distinguishes "we check the role" from "the
database enforces the role", and those are not the same claim.

---

## 5. Production state at time of writing

| | |
|---|---|
| Tests | 351 |
| App schema tables | users, sessions, human\_labels, audit\_log, automation\_freeze |
| Database roles | bellwether\_writer (pipeline), bellwether\_readonly (public API), bellwether\_app (serving container) |
| Sign-in mechanism | Admin-issued passwords, scrypt-hashed, shown once |
| Session cookie | HttpOnly, Secure, SameSite=Lax; first-party via API proxy |
| Audit log entries | written on sign-in, sign-out, label (allowed and refused), freeze (set) |
| Freeze state | unfrozen |
| Session pruning | via `landing.prune_expired` in the maintenance job |

---

## 6. Outstanding

| | |
|---|---|
| **Audit log seal manifest** | M6-FR-37: the audit log shall join the monthly seal manifest before it is pruned. Not built; audit rows accumulate without bound. Deferred to M7 |
| Keyboard/contrast formal pass | D-11: construction makes the queue keyboard-operable; no axe run or WCAG audit has been performed |
| Human labels in training | collected and isolated; M7 studies their agreement and feeds them into retraining |
| No human label has been recorded in production | the queue exists; no reviewer account has judged an edit yet |
