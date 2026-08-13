"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";

// Render's free tier stops the container after a quiet period, and the first
// request afterwards waits for a cold start. Fifty seconds is not unusual.
// The loading state is therefore DETERMINATE — a page that shows a spinner for
// most of a minute with no explanation is indistinguishable from a broken one,
// and the honest fix is to say what is happening rather than to hide it.
const COLD_START_HINT_AFTER_SECONDS = 6;
const COLD_START_BUDGET_SECONDS = 75;

// A job is late once it has missed several scheduled runs, not one. GitHub's
// cron is best-effort and a single skipped slot is normal (SRS R-5).
const STALE_MINUTES = { ingest: 35, label: 95 } as const;

type Run = {
  job: string;
  status: string;
  last_run: string | null;
  minutes_ago: number | null;
};

type Mature = {
  stratum: string;
  n: number;
  reverted: number;
  rate: number | null;
};

type Stats = {
  totals: {
    events: number;
    events_logged_out: number;
    reverted: number;
    labels: number;
    label_checks: number;
    newest_event: string | null;
    oldest_event: string | null;
  };
  mature_48h: Mature[];
  runs: Run[];
};

function freshness(job: string, minutesAgo: number | null) {
  if (minutesAgo === null) return { cls: "muted", text: "never run" };
  const limit = STALE_MINUTES[job as keyof typeof STALE_MINUTES] ?? 60;
  const text = minutesAgo < 1 ? "just now" : `${minutesAgo} min ago`;
  if (minutesAgo > limit * 3) return { cls: "bad", text };
  if (minutesAgo > limit) return { cls: "warn", text };
  return { cls: "ok", text };
}

export default function Page() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [waited, setWaited] = useState(0);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setWaited(0);
    timer.current = setInterval(() => setWaited((s) => s + 1), 1000);
    try {
      const response = await fetch(`${API}/stats`, { cache: "no-store" });
      if (!response.ok) throw new Error(`API returned ${response.status}`);
      setStats((await response.json()) as Stats);
    } catch (err) {
      setError(err instanceof Error ? err.message : "unreachable");
    } finally {
      if (timer.current) clearInterval(timer.current);
    }
  }, []);

  useEffect(() => {
    void load();
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [load]);

  if (!API) {
    return (
      <main>
        <h1>Bellwether</h1>
        <p className="bad">NEXT_PUBLIC_API_BASE_URL is not set.</p>
        <p className="note">
          It is inlined at build time, so setting it requires a redeploy, not
          just a save.
        </p>
      </main>
    );
  }

  const totals = stats?.totals;
  const overallRate =
    totals && totals.events > 0 ? (totals.reverted / totals.events) * 100 : null;

  return (
    <main>
      <h1>Bellwether</h1>
      <p className="tagline">
        A model that notices it is getting worse, and replaces itself.
      </p>

      {!stats && !error && (
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
                width: `${Math.min(
                  100,
                  (waited / COLD_START_BUDGET_SECONDS) * 100,
                )}%`,
              }}
            />
          </div>
        </div>
      )}

      {error && (
        <div className="loading">
          <div className="bad">Could not reach the API — {error}</div>
          <p className="note">
            <button onClick={() => void load()}>Try again</button>
          </p>
        </div>
      )}

      {stats && totals && (
        <>
          <h2>Ingested</h2>
          <div className="grid">
            <div className="card">
              <div className="value">{totals.events.toLocaleString()}</div>
              <div className="label">edits</div>
            </div>
            <div className="card">
              <div className="value">
                {totals.events_logged_out.toLocaleString()}
              </div>
              <div className="label">logged out</div>
            </div>
            <div className="card">
              <div className="value">{totals.label_checks.toLocaleString()}</div>
              <div className="label">outcome checks</div>
            </div>
            <div className="card">
              <div className="value">{totals.labels.toLocaleString()}</div>
              <div className="label">labels</div>
            </div>
          </div>
          <p className="note">
            {overallRate !== null && (
              <>
                {overallRate.toFixed(2)}% of all ingested edits currently carry
                a revert tag — a lower bound, since recent edits have not
                finished being reverted.
              </>
            )}
          </p>

          <h2>Revert rate, matured 48h+</h2>
          {stats.mature_48h.length === 0 ? (
            <p className="muted">
              Nothing has matured yet. Rates appear once edits are 48 hours old.
            </p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Editor</th>
                  <th>Sample</th>
                  <th>Reverted</th>
                  <th>Rate</th>
                </tr>
              </thead>
              <tbody>
                {stats.mature_48h.map((row) => (
                  <tr key={row.stratum}>
                    <td>{row.stratum.replace("_", " ")}</td>
                    <td>{row.n.toLocaleString()}</td>
                    <td>{row.reverted.toLocaleString()}</td>
                    <td>
                      {row.rate === null
                        ? "—"
                        : `${(row.rate * 100).toFixed(2)}%`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="note">
            Every rate carries its sample size and the maturity it was measured
            at. A revert rate without a maturity is a lower bound, not a rate.
          </p>

          <h2>Pipeline</h2>
          <table>
            <thead>
              <tr>
                <th>Job</th>
                <th>Last run</th>
                <th>Outcome</th>
              </tr>
            </thead>
            <tbody>
              {stats.runs.map((run) => {
                const f = freshness(run.job, run.minutes_ago);
                return (
                  <tr key={run.job}>
                    <td>{run.job}</td>
                    <td className={f.cls}>{f.text}</td>
                    <td className={run.status === "success" ? "ok" : "bad"}>
                      {run.status}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="note">
            Staleness is stated rather than implied. Counts with no indication
            of when they last changed look identical to counts that stopped
            changing days ago.
          </p>
        </>
      )}

      <footer>
        <a href="https://github.com/Muhammad-Haris-3/Bellwether">Source</a> ·
        M0 — no model exists yet, by design.
      </footer>
    </main>
  );
}
