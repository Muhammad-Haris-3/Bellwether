"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  API,
  API_CONFIGURED,
  COLD_START_BUDGET_SECONDS,
  COLD_START_HINT_AFTER_SECONDS,
} from "../lib/api";

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
  weighted_n: number;
  weighted_rate: number | null;
};

type Stats = {
  totals: {
    events: number;
    events_logged_out: number;
    tagged_at_ingest: number;
    labels: number;
    label_checks: number;
    revert_events: number;
    newest_event: string | null;
    oldest_event: string | null;
  };
  mature_48h: Mature[];
  runs: Run[];
};

// The offline backtest. A rehearsal: a backfilled census, labels harvested at
// leisure, folds trained on data adjacent to what they score.
type Kc2 = {
  decided: boolean;
  clears_kc2: boolean;
  provisional: boolean;
  margin: number;
  margin_required: number;
  n_events: number;
  maturity_hours: number;
  pr_auc: { model: number; logged_out: number };
};

type MetricRow = {
  n: number;
  n_positives: number;
  pr_auc: number | null;
  pr_auc_ci: [number | null, number | null];
  margin: number | null;
  liftwing: {
    n: number;
    liftwing_pr_auc: number | null;
    model_pr_auc_on_paired: number | null;
    margin: number | null;
  };
};

type Window = {
  maturity_hours: number;
  provisional: boolean;
  excluded: { immature: number; late: number; late_base_rate: number | null };
  aggregate: MetricRow | null;
};

// The performance. Forecasts committed to an append-only register before the
// answer existed, on live data, under a scoring lag.
type Metrics = {
  computed: boolean;
  populations?: Record<
    string,
    { maturity_hours: number; windows: Record<string, Window> }
  >;
  liftwing_last_attempt: {
    status: string;
    requested: number;
    fetched: number;
  } | null;
};

function pct(value: number | null | undefined, digits = 4) {
  return value === null || value === undefined ? "—" : value.toFixed(digits);
}

function statusClass(status: string) {
  if (status === "success") return "ok";
  if (status === "partial" || status === "running") return "warn";
  return "bad";
}

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
  const [kc2, setKc2] = useState<Kc2 | null>(null);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
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

      // The performance figures load separately and are allowed to fail on
      // their own. A page that goes blank because one endpoint is unhappy
      // reports the whole system as down, which is a worse lie than a missing
      // section.
      const [k, m] = await Promise.allSettled([
        fetch(`${API}/kc2`, { cache: "no-store" }).then((r) => r.json()),
        fetch(`${API}/metrics`, { cache: "no-store" }).then((r) => r.json()),
      ]);
      if (k.status === "fulfilled") setKc2(k.value as Kc2);
      if (m.status === "fulfilled") setMetrics(m.value as Metrics);
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

  if (!API_CONFIGURED) {
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
    totals && totals.events > 0
      ? (totals.tagged_at_ingest / totals.events) * 100
      : null;

  return (
    <main>
      <nav className="crumbs">
        <span aria-current="page">Status</span> ·{" "}
        <Link href="/timeline">Timeline</Link> · <Link href="/metrics">Performance</Link> ·{" "}
        <Link href="/queue">Queue</Link> · <Link href="/about">About</Link>
      </nav>

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
            <div className="card">
              <div className="value">
                {totals.revert_events.toLocaleString()}
              </div>
              <div className="label">reverts seen</div>
            </div>
          </div>
          <p className="note">
            {overallRate !== null && (
              <>
                {overallRate.toFixed(2)}% of ingested edits already carried a
                revert tag when we first saw them. A floor, not a rate — the
                tag array is frozen at ingestion, so for a live edit it cannot
                yet know about a revert that has not happened.
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
                  <th>Population</th>
                  <th>Weighted rate</th>
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
                    <td className="muted">
                      {row.weighted_n.toLocaleString()}
                    </td>
                    <td>
                      {row.weighted_rate === null
                        ? "—"
                        : `${(row.weighted_rate * 100).toFixed(2)}%`}
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
          <p className="note">
            Logged-out edits are sampled at 50% and registered ones at 3%, so
            the sample is not the population. Both rates are shown — publishing
            only one would mean choosing which.
          </p>

          {/*
            M4-FR-24. Two PR-AUC figures exist and they measure DIFFERENT
            POPULATIONS. Showing them without that distinction is how a project
            ends up quoting whichever is higher, so they are labelled, put in
            separate columns, and the note under them says it in words rather
            than leaving it to be inferred from a heading.
          */}
          <h2>How good is it?</h2>
          <table>
            <thead>
              <tr>
                <th>Measurement</th>
                <th>PR-AUC</th>
                <th>vs logged-out</th>
                <th>Sample</th>
                <th>Matured at</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>
                  Backtest <span className="muted">(offline, backfilled)</span>
                </td>
                <td>{kc2 ? pct(kc2.pr_auc.model) : "—"}</td>
                <td>{kc2 ? `+${pct(kc2.margin)}` : "—"}</td>
                <td>{kc2 ? kc2.n_events.toLocaleString() : "—"}</td>
                <td className="muted">
                  {kc2 ? `${kc2.maturity_hours}h` : "—"}
                  {kc2?.provisional && " · provisional"}
                </td>
              </tr>
              {["all", "maturity_cohort"].map((population) => {
                const block = metrics?.populations?.[population];
                const window = block?.windows?.["all"];
                const agg = window?.aggregate;
                return (
                  <tr key={population}>
                    <td>
                      Live{" "}
                      <span className="muted">
                        {population === "all"
                          ? "(register, all events)"
                          : "(register, maturity cohort)"}
                      </span>
                    </td>
                    <td>
                      {agg?.pr_auc == null ? (
                        <span className="muted">not yet</span>
                      ) : (
                        <>
                          {pct(agg.pr_auc)}{" "}
                          <span className="muted">
                            [{pct(agg.pr_auc_ci[0])}, {pct(agg.pr_auc_ci[1])}]
                          </span>
                        </>
                      )}
                    </td>
                    <td>{agg?.margin == null ? "—" : pct(agg.margin)}</td>
                    <td>
                      {agg ? agg.n.toLocaleString() : "—"}
                      {agg && agg.n > 0 && (
                        <span className="muted"> · {agg.n_positives} pos</span>
                      )}
                    </td>
                    <td className="muted">
                      {block ? `${block.maturity_hours}h` : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="note">
            These are not the same measurement and the higher one is not the
            answer. The backtest scores a backfilled census whose labels were
            harvested at leisure, with folds trained on data adjacent to what
            they score. The live rows grade forecasts written to an append-only
            register <em>before the outcome existed</em>, on a different
            population, under a scoring lag. Where they disagree, the live one
            is true.
          </p>
          <p className="note">
            The two live rows are also not interchangeable. The maturity cohort
            is the tenth of events the labeller checks densely enough to grade
            at 48 hours; everything else waits for its seven-day check. The
            cohort figure arrives first over far fewer events.
          </p>
          {metrics?.populations?.all?.windows?.["all"] && (
            <p className="note">
              Excluded from the live figures:{" "}
              {metrics.populations.all.windows["all"].excluded.immature.toLocaleString()}{" "}
              not yet matured, and{" "}
              {metrics.populations.all.windows["all"].excluded.late.toLocaleString()}{" "}
              scored after their own outcome was already visible. The second
              exclusion is correct and is not neutral — those edits were
              reverted fast, so dropping them removes real positives, which is
              why their own base rate is published beside the count.
            </p>
          )}

          <h3>Against Wikimedia&rsquo;s own model</h3>
          {(() => {
            const paired =
              metrics?.populations?.all?.windows?.["all"]?.aggregate?.liftwing;
            const attempt = metrics?.liftwing_last_attempt;
            if (paired?.margin != null) {
              return (
                <p>
                  Bellwether {pct(paired.model_pr_auc_on_paired)} against Lift
                  Wing {pct(paired.liftwing_pr_auc)} on {paired.n} events both
                  models scored — a margin of {pct(paired.margin)}.
                </p>
              );
            }
            return (
              <p className="muted">
                No comparison yet
                {paired ? ` — ${paired.n} paired events so far` : ""}.
                {attempt &&
                  ` Last fetch: ${attempt.fetched} of ${attempt.requested} (${attempt.status}).`}
              </p>
            );
          })()}
          <p className="note">
            Wikimedia runs <code>revertrisk-language-agnostic</code> in
            production against the same edits. The SRS recorded, before any
            model here existed, that it is expected to win — and the comparison
            is published whichever way it falls. It is paired on the events both
            models scored, which is why the Bellwether figure quoted here is not
            the one in the table above.
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
                    {/*
                      "partial" is a defined, healthy outcome — the labelling
                      job reports it when some revisions have been deleted and
                      cannot be labelled either way. Colouring anything that is
                      not "success" as a failure turned a normal condition into
                      a red alarm, which is the fastest way to teach someone to
                      ignore the colour.
                    */}
                    <td className={statusClass(run.status)}>{run.status}</td>
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
        M6 — the queue is live. Every figure here is provisional while the
        maturity window is a placeholder.
      </footer>
    </main>
  );
}
