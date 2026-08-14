# Bellwether — M6 Spec

**The application.**
Authentication, roles enforced by row-level security, a live triage queue, human
labels, an immutable audit log, and public timeline and metrics pages.

---

## 1. What M6 is for

M0–M5 is a complete and defensible project. It ingests honestly, predicts before
the outcome exists, grades itself, and maintains itself under a rule fixed
before any model existed. Nobody can use it.

M6 is the layer that turns a pipeline into a product: a queue a reviewer works
through, a record of what they judged, and pages a stranger can read without an
account. SRS §1 promised "a multi-user web application whose reviewers'
judgements feed back into training" — the feedback itself is M7; M6 builds the
surface that collects it.

**This is the first milestone where the work is not primarily about not fooling
myself.** Every one before it was: leak guards, maturity windows, pre-registered
thresholds, append-only evidence. M6's risks are different in kind — a session
that can be stolen, a role that is enforced in the wrong place, a queue that
shows a reviewer an unmatured item and lets them believe it is a result.

That change of risk is worth saying out loud, because the habits that made M0–M5
careful do not transfer automatically. A leak guard does not stop a session
fixation bug.

---

## 2. What M5 handed over

| # | Inherited | Consequence for M6 |
|---|---|---|
| I-1 | The champion is chosen by a decision log, not by recency | The queue must rank by the **serving** champion, and follow a rollback within one scoring cycle |
| I-2 | Predictions carry `role`, and shadow rows exist | The queue must filter to `role = 'champion'` or it will show two scores per edit |
| I-3 | Maturity is 7 days for most events | Almost everything in a live queue is immature, which is exactly what FR-41 is about |
| I-4 | Every evidential table is append-only **by grant** | The app's database role must not be able to write to any of them |
| I-5 | Render's free tier sleeps | "Refreshing without a page reload" meets a container that may take 50 seconds to wake |

---

## 3. Authentication

| # | Requirement |
|---|---|
| M6-FR-1 | Authentication shall be email-based with sessions stored **server-side**, per SRS FR-38 |
| M6-FR-2 | A session shall be a random 256-bit token, stored **hashed**, with the plaintext never persisted anywhere |
| M6-FR-3 | Session cookies shall be `HttpOnly`, `Secure`, `SameSite=Lax`, with an absolute expiry and an idle expiry |
| M6-FR-4 | Sign-in shall be rate-limited per email and per IP, and the response shall not reveal whether an address is registered |
| M6-FR-5 | Signing out shall delete the session row, not merely clear the cookie |
| M6-FR-6 | No endpoint shall ever return a session token, a password hash, or an email address belonging to another user |

M6-FR-2 matters because a session table is a credential store. If the plaintext
token is stored, a database read is a login as anyone — and this project has
already published a database host in a health endpoint on purpose.

### 3.1 The email problem, stated rather than assumed

Email-based sign-in needs something that sends email. Every option has a cost or
a catch, and NFR-1 forbids paying:

| Option | Cost | Catch |
|---|---|---|
| Magic link via a transactional provider | Free tier | Requires an account and an API key; free tiers throttle and can be withdrawn |
| Magic link via Gmail SMTP | Free | App passwords, deliverability, and a personal mailbox in the loop |
| Invite-only, admin-issued credentials | Free | Not email-based sign-in; a deviation from SRS FR-38 that must be recorded |

**Decided 2026-08-14, before implementation: option three.** Accounts are issued
by an administrator with a strong generated password shown once — the same
pattern `scripts/bootstrap_database.py` already uses for database roles. No
email is sent, so NFR-1 is kept without depending on a free tier that can be
withdrawn.

SRS FR-38 is amended to match, with the reasoning and the cost recorded there.
What is given up: self-service sign-up, password reset by email, and any route
to an account for someone the administrator has not met. For a handful of
reviewers that is acceptable; for a real deployment it would not be.

| # | Requirement |
|---|---|
| M6-FR-38 | Passwords shall be hashed with a memory-hard KDF from the standard library — `hashlib.scrypt` — so the serving image gains no dependency |
| M6-FR-39 | A generated password shall be displayed once at creation and never recoverable afterwards |
| M6-FR-40 | Account creation shall itself be an audited admin action |

---

## 4. Roles and row-level security

SRS FR-39: three roles — `viewer`, `reviewer`, `admin` — enforced **at the
database layer via row-level security, not in application code alone**.

| # | Requirement |
|---|---|
| M6-FR-7 | The app shall connect as a dedicated `bellwether_app` role, distinct from `bellwether_writer` and `bellwether_readonly` |
| M6-FR-8 | `bellwether_app` shall hold **no** write privilege on any evidential table — `predictions`, `model_decisions`, `labels`, `rc_events`, `evaluations`, `seals` |
| M6-FR-9 | Every request shall set the acting user in a transaction-local setting, and RLS policies shall read that setting |
| M6-FR-10 | Policies shall be verified by a test that connects **as the role** and attempts the forbidden action, as `sql/002` already does for the writer |
| M6-FR-11 | Removing the application's own role checks shall not grant access — demonstrated by a test that bypasses them |

M6-FR-11 is the one that makes FR-39 mean anything. "Enforced in the database"
is a claim about what happens when the application layer is wrong, and the only
way to show it is to be wrong on purpose and watch the database refuse.

### 4.1 NFR-8 is a constraint on this milestone, not a past achievement

NFR-8: *no credential with write access to `predictions` or `model_decisions`
shall ever be held by the serving API container.* M6 is the first milestone that
gives the serving container **any** write capability at all — human labels and
audit rows — so this is where that guarantee is either kept or quietly lost.

`bellwether_app` gets `INSERT` on exactly two tables and `SELECT` on the rest.
Nothing else, ever.

---

## 5. The queue

| # | Requirement |
|---|---|
| M6-FR-12 | The queue shall rank recent events by the **serving** champion's score, following a rollback within one scoring cycle |
| M6-FR-13 | It shall show only `role = 'champion'` predictions; shadow scores are never displayed |
| M6-FR-14 | Every item shall be labelled **immature** or **matured**, and an immature item shall never be presented as evidence of accuracy (SRS FR-41) |
| M6-FR-15 | It shall refresh without a page reload, by polling, and shall state when it last succeeded |
| M6-FR-16 | It shall be keyboard-operable end to end, with visible focus and screen-reader labels (NFR-12) |

### 5.1 Almost everything in the queue is immature, and that is the point

Maturity is seven days. A queue of recent edits is therefore almost entirely
items whose outcome nobody knows — which is correct, because that is what a
triage queue is for, and it is also the single easiest place in this project to
mislead someone.

A reviewer who sees a score of 0.92 next to an edit and no maturity marker will
read it as *this edit was bad*. It means *this model thinks this edit is likely
to be reverted, and nobody knows yet*. FR-41 exists for that sentence.

| # | Requirement |
|---|---|
| M6-FR-17 | Accuracy figures shall never be computed over queue items; the queue links to `/metrics`, which computes over matured predictions only |

### 5.2 Polling, because the container sleeps

Render's free tier stops the API after inactivity, and the first request
afterwards can take fifty seconds. Server-sent events and websockets against a
sleeping container produce a UI that looks broken.

The queue polls, states its last successful refresh, and shows the same
determinate cold-start indicator the status page already uses. Honest staleness
over a live-looking lie (NFR-11).

---

## 6. Human labels

| # | Requirement |
|---|---|
| M6-FR-18 | A reviewer shall record a judgement with identity, timestamp and confidence (SRS FR-42) |
| M6-FR-19 | Human labels shall live in their own table and shall **never** be written to `outcome.labels` |
| M6-FR-20 | A human label shall record the champion version and the score shown at the time |
| M6-FR-21 | Human labels shall not enter any published accuracy figure in M6 |

M6-FR-19 and FR-21 are the same concern twice. `outcome.labels` holds what
Wikipedia did; a human label holds what a reviewer thought. Mixing them would
make every number this project has published unverifiable, because nobody could
tell afterwards which rows came from which source.

M7 studies their agreement and feeds them into training. M6 collects them and
keeps them apart.

M6-FR-20 exists because a reviewer's judgement is partly a reaction to the score
they were shown. A label collected under one champion is not interchangeable
with one collected under another, and by M7 that distinction is unrecoverable
unless it is recorded now.

---

## 7. The audit log

| # | Requirement |
|---|---|
| M6-FR-22 | Every role-sensitive and state-changing action shall append to `audit_log` (SRS FR-43) |
| M6-FR-23 | The log shall be append-only by grant, like every other evidential table |
| M6-FR-24 | It shall record actor, action, target, outcome and time — including **failed** attempts |
| M6-FR-25 | An admin shall be able to freeze automation, and shall **not** be able to alter, delete or backdate any past decision (SRS FR-37) |

M6-FR-24's failed attempts are the point of an audit log. A log of successful
actions describes what the system did; a log including refusals describes what
it was asked to do, and the second is what shows an access control working.

---

## 8. Public pages

| # | Requirement |
|---|---|
| M6-FR-26 | A public model timeline shall show every promotion, rejection and rollback with its evidence, without authentication (SRS FR-44) |
| M6-FR-27 | A public metrics page shall show current and historical out-of-sample performance, with the maturity window and sample size on every figure (SRS FR-45, NFR-10) |
| M6-FR-28 | The read-only JSON API shall continue to require no authentication (SRS FR-46) |
| M6-FR-29 | Nothing on a public page shall identify a reviewer |

M6-FR-26 is largely done: `/decisions` already serves it. M6 gives it a page.

M6-FR-29 is not in the SRS and is added here. A public timeline showing who
judged what turns a portfolio project into a system that publishes named
individuals' opinions about strangers' edits.

---

## 9. Security

The first milestone with an attack surface. Stated explicitly because the
project's existing habits do not cover it.

| # | Requirement |
|---|---|
| M6-FR-30 | State-changing requests shall carry a CSRF token bound to the session |
| M6-FR-31 | All user-supplied content shall be escaped on output; no `dangerouslySetInnerHTML` |
| M6-FR-32 | Sign-in, sign-out and label endpoints shall be rate-limited independently of read endpoints |
| M6-FR-33 | Errors shall not leak whether a record exists, a user is registered, or a query matched |
| M6-FR-34 | No secret shall appear in any response, log line or error message (NFR-9) |
| M6-FR-35 | Session and CSRF tokens shall be generated with `secrets`, never with `random` |

---

## 10. Storage

| | |
|---|---|
| Users, sessions | trivial; sessions pruned on expiry |
| Human labels | ~100 B a row, bounded by how much a human can read |
| Audit log | the only one that grows without a human bound; pruned at 90 days after sealing |

| # | Requirement |
|---|---|
| M6-FR-36 | Expired sessions shall be deleted by the maintenance job, not left to accumulate |
| M6-FR-37 | The audit log shall join the monthly seal manifest before it is pruned |

---

## 11. Acceptance criteria

| # | |
|---|---|
| D-1 | A user can sign in, and the session is server-side and hashed at rest |
| D-2 | Each of `viewer`, `reviewer`, `admin` is demonstrated seeing and doing exactly what its role allows |
| D-3 | **With the application's own role checks removed, the database still refuses** — the proof that FR-39 is real |
| D-4 | `bellwether_app` is proven unable to write to any evidential table, on the production server |
| D-5 | The queue ranks by the serving champion and follows a rollback |
| D-6 | Every queue item states matured or immature, and no accuracy figure is computed over the queue |
| D-7 | A reviewer's judgement is recorded with identity, confidence, champion version and the score shown |
| D-8 | A failed authorisation attempt appears in the audit log |
| D-9 | An admin can freeze automation and cannot alter a past decision — both demonstrated |
| D-10 | Public timeline and metrics pages load without authentication and identify no reviewer |
| D-11 | The queue is fully keyboard-operable and passes contrast checks |

D-3 is the criterion that matters. Everything else can be satisfied by careful
application code; only D-3 distinguishes "we check the role" from "the database
enforces the role".

---

## 12. The one decision that blocks work

**How does a user sign in?** §3.1 lists three options. The first two need an
account with an email provider and an API key in Render's environment; the third
changes a requirement the SRS states.

I am not choosing this unilaterally: two of the three cost something or depend
on a free tier that can be withdrawn, and the third is a documented deviation
from FR-38. Everything else in M6 can be built either way — schema, roles, RLS,
queue, audit log, public pages — so work can start on all of it while this is
decided.

---

## 13. What M6 must not become

**A place where evidence gets written.** The app collects human judgements. It
does not touch `outcome.labels`, `register.predictions`, or
`decide.model_decisions`, and its database role makes that structural rather
than careful.

**A queue that implies accuracy.** Almost every item in it is immature. A
reviewer who leaves believing the model is 92% right about an edit nobody has
checked has been misled by this project, and no leak guard upstream prevents it.

**Security by application code.** FR-39 says the database enforces the roles. If
the only thing standing between a viewer and a reviewer's action is an `if`
statement in Python, the requirement is not met however correct that statement
is.

**A reason to stop the pipeline.** M0–M5 keeps running throughout. The
application reads what the pipeline produces; it never becomes a path by which a
human quietly adjusts what the pipeline concluded.
