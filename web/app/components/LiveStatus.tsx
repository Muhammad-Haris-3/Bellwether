"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { StatusBody } from "./StatusView";
import {
  API,
  COLD_START_BUDGET_SECONDS,
  COLD_START_HINT_AFTER_SECONDS,
} from "../../lib/api";
import type { Kc2, Metrics, Stats, StatusSnapshot } from "../../lib/status";

// The fallback. What this page used to do on every visit, and now only does
// when the server had no cached copy to serve — the first request after a
// deployment, or a stretch where the API could not be reached at all.
//
// It is kept rather than replaced because the alternative is an error page. A
// visitor who arrives during that window gets the old behaviour, which is slow
// but honest about being slow, instead of nothing.
export function LiveStatus() {
  const [snapshot, setSnapshot] = useState<StatusSnapshot | null>(null);
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
      const stats = (await response.json()) as Stats;

      // The performance figures load separately and are allowed to fail on
      // their own. A page that goes blank because one endpoint is unhappy
      // reports the whole system as down, which is a worse lie than a missing
      // section.
      const [k, m] = await Promise.allSettled([
        fetch(`${API}/kc2`, { cache: "no-store" }).then((r) => r.json()),
        fetch(`${API}/metrics`, { cache: "no-store" }).then((r) => r.json()),
      ]);

      setSnapshot({
        stats,
        kc2: k.status === "fulfilled" ? (k.value as Kc2) : null,
        metrics: m.status === "fulfilled" ? (m.value as Metrics) : null,
        fetchedAt: new Date().toISOString(),
      });
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

  if (snapshot) return <StatusBody snapshot={snapshot} />;

  if (error) {
    return (
      <div className="loading">
        <div className="bad">Could not reach the API — {error}</div>
        <p className="note">
          <button onClick={() => void load()}>Try again</button>
        </p>
      </div>
    );
  }

  return (
    <div className="loading">
      <div>Waking the API… {waited}s</div>
      {waited >= COLD_START_HINT_AFTER_SECONDS && (
        <p className="note">
          The API runs on a free tier that stops when idle. A cold start takes up
          to a minute. This is the first request after a quiet period, not a
          failure.
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
  );
}
