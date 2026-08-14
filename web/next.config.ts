import type { NextConfig } from "next";

// The API is proxied through this origin rather than called across origins.
//
// The session cookie is the reason. Vercel and Render are different registrable
// domains, so a cookie set by the API is a THIRD-PARTY cookie to this page —
// and Chrome blocks those by default now. Sign-in succeeded, the browser
// discarded the cookie, and the next request was anonymous again: the form
// simply returned to itself with no error, because nothing had failed.
//
// SameSite=None; Secure would have worked until the third-party cookie
// phase-out caught up with it. Proxying makes the cookie first-party, which is
// durable rather than a reprieve, and removes the browser's CORS involvement
// entirely as a side effect.
const API_ORIGIN = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "").replace(
  /\/+$/,
  "",
);

const nextConfig: NextConfig = {
  reactStrictMode: true,

  async rewrites() {
    // No destination configured is a real state — the build must not fail, and
    // the page says so itself rather than 404ing on every request.
    if (!API_ORIGIN) return [];
    return [
      {
        source: "/api/:path*",
        destination: `${API_ORIGIN}/:path*`,
      },
    ];
  },
};

export default nextConfig;
