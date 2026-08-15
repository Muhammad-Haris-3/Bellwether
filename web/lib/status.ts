// The shapes the status page reads, and the server-side way of getting them.
//
// The browser-side client lives in ./api.ts and is still what the fallback
// uses. This file is the other half: the same three endpoints, fetched on
// Vercel instead of in the visitor's browser, so the figures can be baked into
// the HTML before it is sent.
//
// Why this exists at all: the API sleeps. Render stops a free instance after
// fifteen minutes of quiet, and the first request afterwards waits about a
// minute for it to come back. Every visitor arriving after a quiet period used
// to watch a progress bar for most of that minute — and the visitor who matters
// most is the one who has never been here before and has no reason to wait.
//
// The obvious fix is to keep the instance awake with a ping. It is a trap. Free
// instance hours are 750 per WORKSPACE per month and a calendar month is about
// 730 hours, so holding one service open around the clock consumes nearly the
// entire allowance and suspends the others when it runs out. Staying inside the
// budget means letting the API sleep and not making the visitor wait for it.

const ORIGIN = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "").replace(/\/+$/, "");

export const API_CONFIGURED = Boolean(ORIGIN);

// How long a cached copy is served before Next.js refreshes it in the
// background. Statically analysable on purpose — the route segment config
// rejects an expression, so this is a literal rather than 5 * 60.
export const REVALIDATE_SECONDS = 300;

// Long enough for a cold start to finish, because the visitor is not the one
// waiting. A refresh runs in the background while the previous copy continues
// to be served, so the only request that ever pays this is the first one after
// a deployment, when there is no previous copy to serve. Cutting it short
// instead would mean the refresh times out every time the instance is asleep
// and the page slowly goes stale.
const FETCH_TIMEOUT_MS = 50_000;

export type Run = {
  job: string;
  status: string;
  last_run: string | null;
  minutes_ago: number | null;
};

export type Mature = {
  stratum: string;
  n: number;
  reverted: number;
  rate: number | null;
  weighted_n: number;
  weighted_rate: number | null;
};

export type Stats = {
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
export type Kc2 = {
  decided: boolean;
  clears_kc2: boolean;
  provisional: boolean;
  margin: number;
  margin_required: number;
  n_events: number;
  maturity_hours: number;
  pr_auc: { model: number; logged_out: number };
};

export type MetricRow = {
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

export type Window = {
  maturity_hours: number;
  provisional: boolean;
  excluded: { immature: number; late: number; late_base_rate: number | null };
  aggregate: MetricRow | null;
};

// The performance. Forecasts committed to an append-only register before the
// answer existed, on live data, under a scoring lag.
export type Metrics = {
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

export type StatusSnapshot = {
  stats: Stats;
  kc2: Kc2 | null;
  metrics: Metrics | null;
  // When the API answered, not when the page was viewed. A figure with no
  // indication of its age is the failure this whole project is about, and
  // caching the page deliberately puts age between the number and the reader.
  fetchedAt: string;
};

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${ORIGIN}${path}`, {
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    next: { revalidate: REVALIDATE_SECONDS },
  });
  if (!response.ok) throw new Error(`API returned ${response.status}`);
  return (await response.json()) as T;
}

// Null means "could not be built", and the caller falls back to fetching in the
// browser exactly as it did before. It never returns a half-empty snapshot:
// a page rendered from nothing would itself be cached and then served for the
// next five minutes, which turns one unlucky moment into a stale empty page.
export async function fetchStatus(): Promise<StatusSnapshot | null> {
  if (!API_CONFIGURED) return null;

  let stats: Stats;
  try {
    stats = await getJson<Stats>("/stats");
  } catch {
    return null;
  }

  // These two are allowed to fail on their own. A page that goes blank because
  // one endpoint is unhappy reports the whole system as down, which is a worse
  // lie than a missing section.
  const [k, m] = await Promise.allSettled([
    getJson<Kc2>("/kc2"),
    getJson<Metrics>("/metrics"),
  ]);

  return {
    stats,
    kc2: k.status === "fulfilled" ? k.value : null,
    metrics: m.status === "fulfilled" ? m.value : null,
    fetchedAt: new Date().toISOString(),
  };
}
