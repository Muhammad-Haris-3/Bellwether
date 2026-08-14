"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import {
  API_CONFIGURED,
  COLD_START_BUDGET_SECONDS,
  COLD_START_HINT_AFTER_SECONDS,
  apiGet,
} from "../../lib/api";

// Public and unauthenticated (SRS FR-45). Every figure carries its sample size
// and the maturity window it was computed at (NFR-10) — a PR-AUC without them
// is not a weaker claim, it is an unreadable one.

type MetricRow = {
  n: number;
  n_positives: number;
  base_rate: number | null;
  weighted_base_rate: number | null;
  pr_auc: number | null;
  pr_auc_ci: [number | null, number | null];
  roc_auc: number | null;
  brier: number | null;
  baseline_pr_auc: number | null;
  margin: number | null;
  segment: string;
  level: string;
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
  segments: MetricRow[];
};

type Metrics = {
  computed: boolean;
  computed_at?: string;
  populations?: Record<
    string,
    { maturity_hours: number; windows: Record<string, Window> }
  >;
  liftwing_last_attempt: { status: string; requested: number; fetched: number } | null;
  note?: string;
};

type Kc2 = {
  margin: number;
  margin_required: number;
  n_events: number;
  maturity_hours: number;
  provisional: boolean;
  clears_kc2: boolean;
  pr_auc: { model: number; logged_out: number };
  ablated_feature: string;
  ablated_margin: number;
};

type Bin = {
  range: [number, number];
  n: number;
  mean_predicted: number | null;
  observed_rate: number | null;
  weighted_observed_rate: number | null;
};

type Calibration = {
  computed: boolean;
  n?: number;
  maturity_hours?: number;
  bins?: Bin[];
  note?: string;
};

type Run = {
  computed_at: string;
  maturity_hours: number;
  n: number;
  n_positives: number;
  pr_auc: number | null;
  pr_auc_ci: [number | null, number | null];
  margin: number | null;
};

type History = { runs: Run[] };

const POPULATIONS = [
  ["all", "every scored event, matured at 7 days"],
  ["maturity_cohort", "the 10% with the full checkpoint grid, matured at 48 hours"],
] as const;

function num(value: number | null | undefined, digits = 4): string {
  return value === null || value === undefined ? "—" : value.toFixed(digits);
}

function when(iso: string | undefined): string {
  return iso ? new Date(iso).toISOString().replace("T", " ").slice(0, 16) + " UTC" : "—";
}

export default function MetricsPage() {
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [kc2, setKc2] = useState<Kc2 | null>(null);
  const [calibration, setCalibration] = useState<Calibration | null>(null);
  const [history, setHistory] = useState<History | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [waited, setWaited] = useState(0);

  const load = useCallback(async () => {
    setError(null);
    setWaited(0);
    const tick = setInterval(() => setWaited((s) => s + 1), 1000);
    try {
      setMetrics(await apiGet<Metrics>("/metrics"));
      // The rest load independently and are allowed to fail alone. A page that
      // goes blank because one endpoint is unhappy reports the whole system as
      // down, which is a worse lie than a missing section.
      const [k, c, h] = await Promise.allSettled([
        apiGet<Kc2>("/kc2"),
        apiGet<Calibration>("/calibration"),
        apiGet<History>("/metrics/history"),
      ]);
      if (k.status === "fulfilled") setKc2(k.value);
      if (c.status === "fulfilled") setCalibration(c.value);
      if (h.status === "fulfilled") setHistory(h.value);
    } catch (err) {
      setError(err instanceof Error ? err.message : "unreachable");
    } finally {
      clearInterval(tick);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (!API_CONFIGURED) {
    return (
      <main>
        <h1>Performance</h1>
        <p className="bad">NEXT_PUBLIC_API_BASE_URL is not set.</p>
      </main>
    );
  }

  return (
    <main>
      <nav className="crumbs">
        <Link href="/">Status</Link> · <Link href="/timeline">Timeline</Link> ·{" "}
        <span aria-current="page">Performance</span> · <Link href="/queue">Queue</Link>
      </nav>

      <h1>Performance</h1>
      <p className="tagline">
        Out-of-sample, on predictions committed before the outcome existed. No
        account required.
      </p>

      {!metrics && !error && (
        <div className="loading">
          <div>Waking the API… {waited}s</div>
          {waited >= COLD_START_HINT_AFTER_SECONDS && (
            <p className="note">
              The API runs on a free tier that stops when idle. A cold start
              takes up to a minute.
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

      {error && (
        <div className="loading">
          <div className="bad">Could not reach the API — {error}</div>
          <p className="note">
            <button onClick={() => void load()}>Try again</button>
          </p>
        </div>
      )}

      {metrics && (
        <>
          {/*
            The two figures side by side, labelled, with a sentence saying they
            measure different populations. Displayed without that distinction is
            how a project ends up quoting whichever is higher — usually without
            meaning to, because by then both are just "the accuracy".
          */}
          <h2>Live, and the backtest it is not</h2>
          <table>
            <thead>
              <tr>
                <th scope="col">Measurement</th>
                <th scope="col">PR-AUC</th>
                <th scope="col">vs logged-out</th>
                <th scope="col">Sample</th>
                <th scope="col">Matured at</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>
                  Backtest <span className="muted">(offline, backfilled census)</span>
                </td>
                <td>{kc2 ? num(kc2.pr_auc.model) : "—"}</td>
                <td>{kc2 ? `+${num(kc2.margin)}` : "—"}</td>
                <td>{kc2 ? kc2.n_events.toLocaleString() : "—"}</td>
                <td className="muted">
                  {kc2 ? `${kc2.maturity_hours} h` : "—"}
                  {kc2?.provisional && " · provisional"}
                </td>
              </tr>
              {POPULATIONS.map(([population, description]) => {
                const block = metrics.populations?.[population];
                const aggregate = block?.windows?.["all"]?.aggregate;
                return (
                  <tr key={population}>
                    <td>
                      Live <span className="muted">({description})</span>
                    </td>
                    <td>
                      {aggregate?.pr_auc == null ? (
                        <span className="muted">not yet</span>
                      ) : (
                        <>
                          {num(aggregate.pr_auc)}{" "}
                          <span className="muted">
                            [{num(aggregate.pr_auc_ci[0])}, {num(aggregate.pr_auc_ci[1])}]
                          </span>
                        </>
                      )}
                    </td>
                    <td>{aggregate?.margin == null ? "—" : num(aggregate.margin)}</td>
                    <td>
                      {aggregate ? aggregate.n.toLocaleString() : "—"}
                      {aggregate && aggregate.n > 0 && (
                        <span className="muted"> · {aggregate.n_positives} pos</span>
                      )}
                    </td>
                    <td className="muted">{block ? `${block.maturity_hours} h` : "—"}</td>
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
            population, under a scoring lag. Where they disagree, the live one is
            true.
          </p>
          <p className="note">
            The two live rows are also not interchangeable. The maturity cohort
            is the tenth of events checked densely enough to grade at 48 hours;
            everything else waits for its seven-day check. The cohort figure
            arrives first over far fewer events.
          </p>

          {kc2 && (
            <>
              <h3>What the margin rests on</h3>
              <p>
                Removing <code>{kc2.ablated_feature}</code> drops the margin from{" "}
                {num(kc2.margin)} to {num(kc2.ablated_margin)}, against a required{" "}
                {num(kc2.margin_required, 2)}.
              </p>
              <p className="note">
                Published because it is the least flattering thing the evaluation
                found. The criterion asks whether a leak-free feature set beats
                the logged-out heuristic by the margin, and it does — adding
                &ldquo;and must survive ablation&rdquo; after seeing the result
                would be exactly the post-hoc criterion change the
                pre-registration exists to forbid. The concentration is recorded
                rather than acted on.
              </p>
            </>
          )}

          <h2>Excluded</h2>
          {(() => {
            const window = metrics.populations?.all?.windows?.["all"];
            if (!window) return <p className="muted">No run yet.</p>;
            return (
              <>
                <p>
                  {window.excluded.immature.toLocaleString()} not yet matured ·{" "}
                  {window.excluded.late.toLocaleString()} scored after their own
                  outcome was already visible
                  {window.excluded.late_base_rate !== null && (
                    <>
                      , of which{" "}
                      {(window.excluded.late_base_rate * 100).toFixed(1)}% were
                      reverts
                    </>
                  )}
                </p>
                <p className="note">
                  The second exclusion is correct and is not neutral. Those
                  predictions concentrate in edits that were reverted fast, so
                  dropping them removes real positives — which is why their own
                  base rate is published beside the count. &ldquo;We excluded
                  4%&rdquo; and &ldquo;we excluded 4% that were 60%
                  positive&rdquo; are different statements about the same number.
                </p>
              </>
            );
          })()}

          <h2>Calibration</h2>
          {!calibration?.computed || !calibration.bins ? (
            <p className="muted">
              No reliability curve yet — it is computed over matured predictions
              only.
            </p>
          ) : (
            <>
              <table>
                <caption className="sr-only">
                  Reliability: predicted probability against observed frequency,
                  with the number of predictions in each band.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Score band</th>
                    <th scope="col">Predictions</th>
                    <th scope="col">Mean predicted</th>
                    <th scope="col">Observed (weighted)</th>
                  </tr>
                </thead>
                <tbody>
                  {calibration.bins.map((bin) => (
                    <tr key={bin.range[0]}>
                      <td className="muted">
                        {bin.range[0].toFixed(1)}–{bin.range[1].toFixed(1)}
                      </td>
                      <td>{bin.n.toLocaleString()}</td>
                      <td>{num(bin.mean_predicted, 3)}</td>
                      <td>{num(bin.weighted_observed_rate, 3)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="note">
                The observed column is population-weighted. The frame keeps 50%
                of logged-out edits and 3% of registered ones, so the raw sample
                frequency is around four times the population&rsquo;s — a model
                calibrated against it would be calibrated to a population that
                does not exist and would overstate risk in production by roughly
                that factor. Empty bands are shown: a band holding four
                predictions and one holding four thousand look identical without
                the count.
              </p>
            </>
          )}

          <h2>Against Wikimedia&rsquo;s own model</h2>
          {(() => {
            const paired = metrics.populations?.all?.windows?.["all"]?.aggregate?.liftwing;
            const attempt = metrics.liftwing_last_attempt;
            if (paired?.margin != null) {
              return (
                <p>
                  Bellwether {num(paired.model_pr_auc_on_paired)} against Lift Wing{" "}
                  {num(paired.liftwing_pr_auc)} on {paired.n} events both models
                  scored — a margin of {num(paired.margin)}.
                </p>
              );
            }
            return (
              <p className="muted">
                No comparison yet{paired ? ` — ${paired.n} paired events so far` : ""}.
                {attempt &&
                  ` Last fetch: ${attempt.fetched} of ${attempt.requested} (${attempt.status}).`}
              </p>
            );
          })()}
          <p className="note">
            Wikimedia runs <code>revertrisk-language-agnostic</code> in production
            against the same edits. The SRS recorded, before any model here
            existed, that it is expected to win — and the comparison is published
            whichever way it falls. It is paired on the events both models
            scored, so the Bellwether figure quoted here is not the one in the
            table above.
          </p>

          <h2>History</h2>
          {!history || history.runs.length === 0 ? (
            <p className="muted">No runs recorded yet.</p>
          ) : (
            <>
              <table>
                <thead>
                  <tr>
                    <th scope="col">Computed</th>
                    <th scope="col">Sample</th>
                    <th scope="col">PR-AUC</th>
                    <th scope="col">Interval</th>
                    <th scope="col">Margin</th>
                  </tr>
                </thead>
                <tbody>
                  {history.runs.slice(0, 20).map((run) => (
                    <tr key={run.computed_at}>
                      <td className="muted">{when(run.computed_at)}</td>
                      <td>
                        {run.n.toLocaleString()}
                        <span className="muted"> · {run.n_positives} pos</span>
                      </td>
                      <td>{num(run.pr_auc)}</td>
                      <td className="muted">
                        {run.pr_auc == null
                          ? "—"
                          : `[${num(run.pr_auc_ci[0])}, ${num(run.pr_auc_ci[1])}]`}
                      </td>
                      <td>{num(run.margin)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="note">
                Every row carries its own sample size. A series of PR-AUC values
                without them looks like a trend when it is mostly the sample
                growing — the early runs are computed over a handful of matured
                predictions and the first ones over none at all. A run with no
                figure is a window that held nothing gradeable, which is a fact
                about the window rather than about the model.
              </p>
            </>
          )}

          <p className="note">
            Last computed {when(metrics.computed_at)}. Every figure on this page
            is provisional while the maturity window is a placeholder.
          </p>
        </>
      )}

      <footer>
        <Link href="/">Status</Link> · <Link href="/timeline">Timeline</Link> ·{" "}
        <Link href="/queue">Queue</Link>
      </footer>
    </main>
  );
}
