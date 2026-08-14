"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { ScrollReveal } from "../components/ScrollReveal";

import {
  API_CONFIGURED,
  ApiError,
  COLD_START_BUDGET_SECONDS,
  COLD_START_HINT_AFTER_SECONDS,
  apiGet,
  apiPost,
} from "../../lib/api";

// Polling, not websockets. Render's free tier stops the container when idle, and
// a socket against a sleeping process produces a UI that looks live and is not.
// The last successful refresh is stated instead (NFR-11).
const POLL_SECONDS = 30;

type Me = {
  authenticated: boolean;
  email?: string;
  role?: string;
  auth_configured: boolean;
};

type Item = {
  revid: number;
  // Null until this reviewer has judged the row. The API withholds them rather
  // than the page hiding them — a value returned and not displayed is readable
  // by anyone with the network tab open.
  score: number | null;
  model_version: string | null;
  event_ts: string;
  title: string;
  user_name: string | null;
  namespace: number;
  is_logged_out: boolean;
  byte_delta: number;
  comment: string | null;
  matured: boolean;
  // Null until the outcome is final. Never false for "not checked yet".
  reverted: boolean | null;
  my_verdict: string | null;
};

type Queue = {
  items: Item[];
  window_hours: number;
  matured: number;
  immature: number;
  maturity_hours: number;
  freshness: { last_scored_at: string | null; newest_event: string | null };
  note: string;
};

// What the API hands back once a verdict is committed: the model's score, and
// the real outcome if it is settled. Shown only now, so the judgement was made
// without either.
type Judged = {
  score: number;
  model_version: string;
  was_matured: boolean;
  reverted: boolean | null;
};

const VERDICTS = [
  { key: "bad_edit", label: "Bad" },
  { key: "good_edit", label: "Good" },
  { key: "unsure", label: "Unsure" },
] as const;

function ago(iso: string | null): string {
  if (!iso) return "never";
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} d ago`;
}

export default function QueuePage() {
  const [me, setMe] = useState<Me | null>(null);
  const [queue, setQueue] = useState<Queue | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [waited, setWaited] = useState(0);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [hours, setHours] = useState(24);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const loadMe = useCallback(async () => {
    setWaited(0);
    const tick = setInterval(() => setWaited((s) => s + 1), 1000);
    try {
      setMe(await apiGet<Me>("/auth/me"));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "unreachable");
    } finally {
      clearInterval(tick);
    }
  }, []);

  const loadQueue = useCallback(async () => {
    try {
      setQueue(await apiGet<Queue>(`/queue?hours=${hours}&limit=50`));
      setLastRefresh(new Date());
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setMe((current) => (current ? { ...current, authenticated: false } : current));
        return;
      }
      setError(err instanceof Error ? err.message : "unreachable");
    }
  }, [hours]);

  useEffect(() => {
    void loadMe();
  }, [loadMe]);

  useEffect(() => {
    if (!me?.authenticated) return;
    void loadQueue();

    // Only while this tab is actually being looked at.
    //
    // A reviewer opens a diff in another tab and comes back a minute later;
    // refreshing underneath them replaces the list they were working through.
    // The row order is stable now, but a poll can still add or drop rows, and
    // there is no reason to refresh a page nobody is reading.
    const tick = () => {
      if (document.visibilityState === "visible") void loadQueue();
    };
    timer.current = setInterval(tick, POLL_SECONDS * 1000);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [me?.authenticated, loadQueue]);

  // Polling pauses while the tab is hidden, so a reviewer coming back from a
  // diff can be looking at a list a few minutes old. This asks for the current
  // one on demand rather than making them wait for the next tick.
  async function refreshNow() {
    setRefreshing(true);
    try {
      await loadQueue();
    } finally {
      setRefreshing(false);
    }
  }

  async function judge(revid: number, verdict: string, confidence: string) {
    setBusy(revid);
    try {
      // Update the row in place from the response rather than refetching.
      //
      // Refetching threw away the reveal: the random slice is redrawn on every
      // request, so the row just judged was often not in the next response at
      // all — the reviewer answered and their row vanished, taking the score
      // and the outcome with it.
      const revealed = await apiPost<Judged>("/labels", { revid, verdict, confidence });
      setQueue((current) =>
        current
          ? {
              ...current,
              items: current.items.map((item) =>
                item.revid === revid
                  ? {
                      ...item,
                      my_verdict: verdict,
                      score: revealed.score,
                      model_version: revealed.model_version,
                      reverted: revealed.reverted,
                      matured: revealed.was_matured,
                    }
                  : item,
              ),
            }
          : current,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not record that");
    } finally {
      setBusy(null);
    }
  }

  if (!API_CONFIGURED) {
    return (
      <main>
        <h1>Queue</h1>
        <p className="bad">NEXT_PUBLIC_API_BASE_URL is not set.</p>
      </main>
    );
  }

  if (!me) {
    return (
      <main>
        {/* The nav belongs here too. This state can last most of a minute on a
            cold start, and a page with no way out of it reads as broken rather
            than as slow. */}
        <nav className="crumbs">
          <Link href="/">Status</Link>
          <Link href="/timeline">Timeline</Link>
          <Link href="/metrics">Performance</Link>
          <span aria-current="page">Queue</span>
        </nav>
        <ScrollReveal>
          <h1>Queue</h1>
        </ScrollReveal>

        {/*
          The error goes ABOVE the waiting state, and the waiting state stops
          claiming to wait once there is one.

          The first version rendered "Waking the API…" whenever `me` was null,
          which is also true when the request FAILED. A CORS rejection then
          looked exactly like a slow container: the page sat at 0s forever and
          said the API was starting up, while the API was awake and answering in
          milliseconds. A loading state that cannot be interrupted by a failure
          is a loading state that lies.
        */}
        {error && (
          <div className="loading">
            <div className="bad">Could not reach the API — {error}</div>
            <p className="note">
              The API answering directly but not from this page usually means
              the browser blocked the request: the deployment&rsquo;s allowed
              origin does not match{" "}
              <code>{typeof window !== "undefined" ? window.location.origin : ""}</code>.
              The browser console will say so.
            </p>
            <p className="note">
              <button onClick={() => void loadMe()}>Try again</button>
            </p>
          </div>
        )}

        {!error && (
          <div className="loading">
            <div>Waking the API… {waited}s</div>
            {waited >= COLD_START_HINT_AFTER_SECONDS && (
              <p className="note">
                The API runs on a free tier that stops when idle. A cold start
                takes up to a minute. This is the first request after a quiet
                period, not a failure.
              </p>
            )}
            <div className="bar">
              <div
                style={{
                  width: `${Math.min(100, (waited / COLD_START_BUDGET_SECONDS) * 100)}%`,
                }}
              />
            </div>
          </div>
        )}
      </main>
    );
  }

  if (!me.authenticated) {
    return <SignIn configured={me.auth_configured} onSignedIn={() => void loadMe()} />;
  }

  return (
    <main>
      <nav className="crumbs">
        <Link href="/">Status</Link>
        <Link href="/timeline">Timeline</Link>
        <Link href="/metrics">Performance</Link>
        <span aria-current="page">Queue</span>
      </nav>

      <ScrollReveal>
        <h1>Triage queue</h1>
        <p className="tagline">
          Signed in as {me.email} ({me.role})
          <button className="linkish" onClick={() => void signOut(loadMe)}>
            sign out
          </button>
        </p>
      </ScrollReveal>

      {/*
        FR-41, stated before the list rather than beside each row alone. A
        reviewer who reads one sentence should already know that a score here is
        what the model expects, not what happened.
      */}
      {queue && (
        <ScrollReveal>
          <div className="callout" role="note">
            <strong>{queue.immature}</strong> of {queue.items.length} items have no
            outcome yet — an edit is only settled after {queue.maturity_hours}{" "}
            hours. <strong>The model&rsquo;s score is hidden until you judge.</strong>{" "}
            An opinion formed after seeing it is agreement with a number rather
            than a judgement about the edit. Part of this list is drawn at random
            rather than by risk, and which part is not disclosed — without it,
            these labels would only ever describe edits the model already flagged.
            Accuracy is measured on matured predictions only, on the{" "}
            <Link href="/metrics">performance page</Link>.
          </div>
        </ScrollReveal>
      )}

      <ScrollReveal className="controls">
        <label htmlFor="window">Window</label>
        <select
          id="window"
          value={hours}
          onChange={(event) => setHours(Number(event.target.value))}
        >
          <option value={24}>last 24 hours</option>
          <option value={72}>last 3 days</option>
          <option value={336}>last 14 days</option>
          <option value={720}>last 30 days</option>
        </select>
        <button onClick={() => void refreshNow()} disabled={refreshing}>
          {refreshing ? "Refreshing…" : "Refresh"}
        </button>
        <span className="note" aria-live="polite">
          refreshed {lastRefresh ? ago(lastRefresh.toISOString()) : "…"} · polls
          every {POLL_SECONDS}s while this tab is open
        </span>
      </ScrollReveal>

      {error && <p className="bad">{error}</p>}

      {queue && queue.items.length === 0 && (
        <p className="muted">
          Nothing scored in this window yet. The scorer runs after each
          ingestion.
        </p>
      )}

      {queue && queue.items.length > 0 && (
        <ScrollReveal>
          <div className="scroller">
<table>
            <caption className="sr-only">
              Recent edits ranked by the serving model&rsquo;s predicted revert
              risk. Each row states whether its outcome is settled.
            </caption>
            <thead>
              <tr>
                <th scope="col">Score</th>
                <th scope="col">Page</th>
                <th scope="col">Editor</th>
                <th scope="col">When</th>
                <th scope="col">Outcome</th>
                <th scope="col">Your call</th>
              </tr>
            </thead>
            <tbody>
              {queue.items.map((item) => (
                <tr key={item.revid}>
                  <td>
                    {item.score === null ? (
                      <span className="note" title="Shown after you judge">
                        hidden
                      </span>
                    ) : (
                      <span className={item.score >= 0.5 ? "warn" : "muted"}>
                        {item.score.toFixed(3)}
                      </span>
                    )}
                  </td>
                  <td>
                    <a
                      href={`https://en.wikipedia.org/wiki/Special:Diff/${item.revid}`}
                      target="_blank"
                      rel="noreferrer noopener"
                    >
                      {item.title}
                    </a>
                    <div className="note">
                      {item.byte_delta >= 0 ? "+" : ""}
                      {item.byte_delta} bytes · rev {item.revid}
                      {item.comment ? ` · ${item.comment.slice(0, 60)}` : ""}
                    </div>
                  </td>
                  <td className={item.is_logged_out ? "warn" : "muted"}>
                    {item.user_name ?? "—"}
                  </td>
                  <td className="muted">{ago(item.event_ts)}</td>
                  <td>
                    {/*
                      Null is not false. "Not settled" and "survived" are
                      different facts, and a queue that showed them the same way
                      would let a reviewer read an unchecked edit as a confirmed
                      good one.
                    */}
                    {item.reverted === null ? (
                      <span className="pending">not settled</span>
                    ) : item.reverted ? (
                      <span className="bad">reverted</span>
                    ) : (
                      <span className="ok">stood</span>
                    )}
                  </td>
                  <td>
                    {item.my_verdict ? (
                      <span className="judged">
                        you said <strong>{item.my_verdict.replace("_", " ")}</strong>
                      </span>
                    ) : me.role === "viewer" ? (
                      <span className="note">read only</span>
                    ) : (
                      <span className="verdicts">
                        {VERDICTS.map((verdict) => (
                          <button
                            key={verdict.key}
                            disabled={busy === item.revid}
                            onClick={() => void judge(item.revid, verdict.key, "medium")}
                            aria-label={`Mark ${item.title} as ${verdict.label}`}
                          >
                            {verdict.label}
                          </button>
                        ))}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
</div>
        </ScrollReveal>
      )}

      <footer>
        <Link href="/">Status</Link>
        <span>
          M6 — the queue. Scores come from the model the decision log promoted.
        </span>
      </footer>
    </main>
  );
}

async function signOut(after: () => void) {
  await apiPost("/auth/sign-out", {});
  after();
}

function SignIn({
  configured,
  onSignedIn,
}: {
  configured: boolean;
  onSignedIn: () => void;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await apiPost("/auth/sign-in", { email, password });
      onSignedIn();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not sign in");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main>
      <nav className="crumbs">
        <Link href="/">Status</Link>
        <span aria-current="page">Sign in</span>
      </nav>
      <ScrollReveal>
        <h1>Sign in</h1>
      </ScrollReveal>

      {!configured ? (
        <ScrollReveal>
          <p className="bad">
            Authentication is not configured on this deployment.
          </p>
        </ScrollReveal>
      ) : (
        <ScrollReveal>
          <form onSubmit={submit} className="signin">
            <label htmlFor="email">Email</label>
            <input
              id="email"
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />

            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />

            <button type="submit" disabled={busy}>
              {busy ? "Checking…" : "Sign in"}
            </button>

            {/* aria-live, so a screen reader announces a failure that appears
                without the focus moving. */}
            <p className="bad" role="alert" aria-live="polite">
              {error ?? ""}
            </p>
          </form>
        </ScrollReveal>
      )}

      <ScrollReveal>
        <p className="note">
          There is no sign-up. Accounts are issued by an administrator — the SRS
          requirement for email sign-in was amended before this was built, because
          sending email needs a service that costs money or can be withdrawn.
        </p>
      </ScrollReveal>
    </main>
  );
}
