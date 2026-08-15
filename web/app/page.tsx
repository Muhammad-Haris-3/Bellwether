import { LiveStatus } from "./components/LiveStatus";
import {
  StatusBody,
  StatusFooter,
  StatusHeader,
} from "./components/StatusView";
import {
  API_CONFIGURED,
  REVALIDATE_SECONDS,
  fetchStatus,
} from "../lib/status";

// The figures are read on the server and baked into the HTML, then that HTML is
// served to everyone for five minutes while a refresh happens in the background.
//
// This page used to fetch from the browser on every visit, which meant every
// visitor after a quiet period watched a progress bar while Render started the
// API back up — up to a minute of nothing, on the first page anyone sees. The
// numbers change every ten minutes at most, so serving a five-minute-old copy
// costs almost no freshness and removes the wait entirely. What it does cost is
// certainty about age, which is why the figures now carry the instant they were
// read (lib/status.ts explains why keeping the API awake instead is a trap).
//
// Written as a literal, and duplicated from REVALIDATE_SECONDS rather than
// imported, because this value is read by the compiler rather than at runtime:
// it has to be statically analysable, and an imported binding is not guaranteed
// to be. The assertion below is what stops the two drifting apart.
export const revalidate = 300;

// The guard. `never` the moment the literal above and the shared constant stop
// agreeing, which makes the file fail to compile rather than quietly caching
// for one interval while fetching on another.
type RevalidateAgrees = typeof REVALIDATE_SECONDS extends 300 ? true : never;
const revalidateAgrees: RevalidateAgrees = true;
void revalidateAgrees;

// The refresh may have to sit through a cold start. It runs in the background
// while the previous copy is still being served, so this ceiling is not a wait
// anyone experiences — but without it the platform's default cuts the refresh
// off long before a sleeping API can answer, and the page never updates.
export const maxDuration = 60;

export default async function Page() {
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

  const snapshot = await fetchStatus();

  return (
    <main>
      <StatusHeader />
      {snapshot ? <StatusBody snapshot={snapshot} /> : <LiveStatus />}
      <StatusFooter />
    </main>
  );
}
