"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { ScrollReveal } from "../components/ScrollReveal";

import {
  API_CONFIGURED,
  COLD_START_BUDGET_SECONDS,
  COLD_START_HINT_AFTER_SECONDS,
  apiGet,
} from "../../lib/api";

// Public, and deliberately unauthenticated (SRS FR-44). The point of a decision
// log is that someone who does not work here can read it.

type Condition = {
  what?: string;
  measured?: number | [number | null, number | null] | null;
  ece_regression?: number | null;
  worst_segment?: string | null;
  worst_segment_regression?: number | null;
  pass: boolean | null;
};

type Decision = {
  decision_id: number;
  decided_at: string;
  decision: "promote" | "reject" | "rollback";
  champion: string;
  challenger: string | null;
  reason: string | null;
  conditions: Record<string, Condition>;
  champion_pr_auc: number | null;
  challenger_pr_auc: number | null;
  n_matured: number | null;
  n_positives: number | null;
  code_commit: string | null;
};

type Evaluation = {
  day: string;
  champion: string;
  rolling_pr_auc: number | null;
  baseline_pr_auc: number | null;
  drop: number | null;
  worst_psi: number | null;
  worst_psi_feature: string | null;
  days_since_train: number | null;
  breached: { decay: boolean; drift: boolean; floor: boolean };
  streaks: { decay: number; drift: number };
  fired: boolean;
  reason: string | null;
  n_matured: number;
};

type Decisions = {
  serving: string | null;
  counts: Record<string, number>;
  decisions: Decision[];
  champion_history: {
    model_version: string;
    effective_from: string;
    replaced: string | null;
  }[];
  recent_trigger_evaluations: Evaluation[];
  note: string;
};

// The rule, quoted, so a reader can check the verdicts against it rather than
// taking them on trust. Fixed in PREREGISTRATION.md before the first model was
// trained, and asserted against that document by the test suite.
const RULE = [
  ["P-1", "PR-AUC exceeds the champion's by at least 0.02, on the same matured events"],
  ["P-2", "the paired bootstrap 95% interval for that difference excludes zero"],
  ["P-3", "at least 2,500 matured positives have accumulated in shadow"],
  ["P-4", "at least 7 days of wall-clock shadow running have elapsed"],
  ["P-5", "ECE no worse by more than 0.02, and no segment regresses by more than 0.03"],
] as const;

function when(iso: string): string {
  return new Date(iso).toISOString().replace("T", " ").slice(0, 16) + " UTC";
}

function measured(condition: Condition): string {
  if (Array.isArray(condition.measured)) {
    const [low, high] = condition.measured;
    return low === null || high === null ? "—" : `[${low}, ${high}]`;
  }
  if (condition.ece_regression !== undefined) {
    const segment = condition.worst_segment
      ? `, worst segment ${condition.worst_segment} ${condition.worst_segment_regression}`
      : "";
    return `ECE ${condition.ece_regression ?? "—"}${segment}`;
  }
  return condition.measured === null || condition.measured === undefined
    ? "—"
    : String(condition.measured);
}

export default function TimelinePage() {
  const [data, setData] = useState<Decisions | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [waited, setWaited] = useState(0);

  const load = useCallback(async () => {
    setError(null);
    setWaited(0);
    const tick = setInterval(() => setWaited((s) => s + 1), 1000);
    try {
      setData(await apiGet<Decisions>("/decisions"));
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
        <h1>Model timeline</h1>
        <p className="bad">NEXT_PUBLIC_API_BASE_URL is not set.</p>
      </main>
    );
  }

  return (
    <main>
      <nav className="crumbs">
        <Link href="/">Status</Link>
        <span aria-current="page">Timeline</span>
        <Link href="/metrics">Performance</Link>
        <Link href="/queue">Queue</Link>
        <Link href="/about">About</Link>
      </nav>

      <ScrollReveal>
        <h1>Model timeline</h1>
        <p className="tagline">
          Every promotion, rejection and rollback, with the evidence that produced
          it. No account required.
        </p>
      </ScrollReveal>

      {!data && !error && (
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

      {data && (
        <>
          <ScrollReveal>
            <h2>The rule</h2>
            <p className="note">
              Fixed in <code>PREREGISTRATION.md</code> before the first model was
              trained. A challenger replaces the champion only if all five hold;
              failing any one it is rejected and the champion stays. The
              thresholds live in code and are asserted against that document by
              the test suite, so the two cannot drift apart.
            </p>
            <table>
              <tbody>
                {RULE.map(([id, text]) => (
                  <tr key={id}>
                    <td className="muted" style={{ whiteSpace: "nowrap" }}>
                      {id}
                    </td>
                    <td>{text}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ScrollReveal>

          <ScrollReveal>
            <h2>Decisions</h2>
          </ScrollReveal>

          {data.decisions.length === 0 ? (
            <ScrollReveal>
              {/*
                An empty log is a real state and says something. It is not a
                page that failed to load, and it is not a system that has never
                been checked — the trigger evaluations below show it being
                checked daily.
              */}
              <p className="muted">
                No decision has been recorded yet. Nothing has been promoted,
                rejected or rolled back.
              </p>
              <p className="note">
                A decision requires a challenger in shadow, and a challenger
                requires a retrain trigger to fire. P-3 needs 2,500 matured
                positives and P-4 needs seven days of shadow running, so the
                earliest possible first decision is roughly two weeks after the
                first challenger appears. The evaluations below are the record
                of that being checked, daily, including the days nothing fired.
              </p>
            </ScrollReveal>
          ) : (
            data.decisions.map((decision) => (
              <ScrollReveal key={decision.decision_id} className="decision">
                <div className="decision-head">
                  <span className={decision.decision === "reject" ? "warn" : "ok"}>
                    {decision.decision.toUpperCase()}
                  </span>{" "}
                  <span className="muted">{when(decision.decided_at)}</span>
                </div>
                <p>
                  {decision.challenger ?? "—"} against {decision.champion}
                  {decision.challenger_pr_auc !== null && (
                    <>
                      {" "}
                      · PR-AUC {decision.challenger_pr_auc} vs{" "}
                      {decision.champion_pr_auc}
                    </>
                  )}
                  {decision.n_matured !== null && (
                    <span className="muted">
                      {" "}
                      · {decision.n_matured.toLocaleString()} matured,{" "}
                      {decision.n_positives?.toLocaleString()} positive
                    </span>
                  )}
                </p>
                {decision.reason && <p className="note">{decision.reason}</p>}
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Condition</th>
                      <th scope="col">Measured</th>
                      <th scope="col">Verdict</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(decision.conditions).map(([id, condition]) => (
                      <tr key={id}>
                        <td className="muted">
                          {id} <span className="note">{condition.what}</span>
                        </td>
                        <td>{measured(condition)}</td>
                        <td className={condition.pass ? "ok" : "bad"}>
                          {condition.pass === null ? "—" : condition.pass ? "pass" : "fail"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </ScrollReveal>
            ))
          )}

          <ScrollReveal>
            <h2>Serving</h2>
            <p>
              {data.serving ? (
                <>
                  <code>{data.serving}</code>
                </>
              ) : (
                <span className="muted">
                  No promotion has happened, so the newest registered model is
                  serving by fallback.
                </span>
              )}
            </p>
          </ScrollReveal>

          <ScrollReveal>
            <h2>Retrain checks</h2>
            {data.recent_trigger_evaluations.length === 0 ? (
              <p className="muted">
                No evaluation recorded yet. The check runs daily.
              </p>
            ) : (
              <table>
                <caption className="sr-only">
                  Daily evaluations of the three pre-registered retrain triggers,
                  including the days on which none fired.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Day</th>
                    <th scope="col">Matured</th>
                    <th scope="col">Rolling PR-AUC</th>
                    <th scope="col">Baseline</th>
                    <th scope="col">Worst PSI</th>
                    <th scope="col">Since training</th>
                    <th scope="col">Fired</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent_trigger_evaluations.map((row) => (
                    <tr key={row.day}>
                      <td className="muted">{row.day}</td>
                      {/* The denominator beside the dash. Without it a row of
                          em-dashes reads as a broken job rather than as a window
                          holding nothing yet to measure. */}
                      <td className="muted">{row.n_matured.toLocaleString()}</td>
                      <td>{row.rolling_pr_auc ?? "—"}</td>
                      <td className="muted">{row.baseline_pr_auc ?? "—"}</td>
                      <td>
                        {row.worst_psi ?? "—"}
                        {row.worst_psi_feature && (
                          <span className="note"> {row.worst_psi_feature}</span>
                        )}
                      </td>
                      <td className="muted">
                        {row.days_since_train === null ? "—" : `${row.days_since_train} d`}
                      </td>
                      <td className={row.fired ? "warn" : "muted"}>
                        {row.fired ? row.reason : "no"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="note">
              A row of dashes is not a failed check. The rolling figure is computed
              over predictions that have matured, and maturity is seven days — so
              until the register is that old there is nothing to measure and the
              matured count says so.
            </p>
            <p className="note">
              Every evaluation is recorded, including the ones that fire nothing. A
              table holding only the firings could not answer &ldquo;was this
              checked yesterday&rdquo;, and a trigger that silently stopped being
              evaluated would look exactly like one that keeps not firing.
            </p>
          </ScrollReveal>

          <ScrollReveal>
            <h2>What has served</h2>
            {data.champion_history.length === 0 ? (
              <p className="muted">
                Nothing has been promoted, so there is no history to show.
              </p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th scope="col">Model</th>
                    <th scope="col">From</th>
                    <th scope="col">Replaced</th>
                  </tr>
                </thead>
                <tbody>
                  {data.champion_history.map((row) => (
                    <tr key={`${row.model_version}-${row.effective_from}`}>
                      <td>
                        <code>{row.model_version}</code>
                      </td>
                      <td className="muted">{when(row.effective_from)}</td>
                      <td className="muted">{row.replaced ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </ScrollReveal>
        </>
      )}

      <footer>
        <Link href="/">Status</Link>
        <span>
          <Link href="/queue">Queue</Link> · Rejections
          are shown as prominently as promotions; a log holding only the changes
          cannot answer what was refused.
        </span>
      </footer>
    </main>
  );
}
