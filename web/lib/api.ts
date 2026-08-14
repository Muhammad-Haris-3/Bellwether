// One place that knows how to talk to the API.
//
// Trailing slashes are stripped rather than forbidden: `${base}/queue` with a
// base ending in "/" produces "//queue", which FastAPI answers with a 404 — a
// failure that reads as "the API is broken" when the API is fine and the
// environment variable has one extra character.
export const API = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "").replace(
  /\/+$/,
  "",
);

// Render's free tier stops the container after a quiet period and the first
// request afterwards waits for a cold start. Fifty seconds is not unusual, so
// every loading state in this app is determinate — a spinner for most of a
// minute is indistinguishable from a broken page.
export const COLD_START_HINT_AFTER_SECONDS = 6;
export const COLD_START_BUDGET_SECONDS = 75;

const CSRF_COOKIE = "bellwether_csrf";
const CSRF_HEADER = "x-csrf-token";

function csrfToken(): string | null {
  // Readable on purpose: the double-submit pattern needs the page to read this
  // value and echo it in a header. The SESSION cookie is HttpOnly and is never
  // visible here, which is the whole point of them being two cookies.
  const match = document.cookie.match(
    new RegExp(`(?:^|; )${CSRF_COOKIE}=([^;]*)`),
  );
  return match ? decodeURIComponent(match[1]) : null;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

// `credentials: "include"` on every call, because the session lives in a cookie
// on a different origin. Without it the browser silently omits it and every
// authenticated request looks like an anonymous one.
export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    cache: "no-store",
    credentials: "include",
  });
  if (!response.ok) {
    throw new ApiError(response.status, await errorText(response));
  }
  return (await response.json()) as T;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const token = csrfToken();
  const response = await fetch(`${API}${path}`, {
    method: "POST",
    cache: "no-store",
    credentials: "include",
    headers: {
      "content-type": "application/json",
      ...(token ? { [CSRF_HEADER]: token } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new ApiError(response.status, await errorText(response));
  }
  return (await response.json()) as T;
}

async function errorText(response: Response): Promise<string> {
  // The API's own `detail` where there is one. It is written to be shown: the
  // sign-in failure says the same thing whether the address is unknown or the
  // password is wrong, deliberately.
  try {
    const body = (await response.json()) as { detail?: string };
    if (typeof body.detail === "string") return body.detail;
  } catch {
    /* not JSON; fall through */
  }
  return `The API returned ${response.status}.`;
}
