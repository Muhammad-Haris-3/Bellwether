import type { Metadata } from "next";
import { IBM_Plex_Sans, JetBrains_Mono } from "next/font/google";

import { Nav } from "./components/Nav";
import "./globals.css";

// Self-hosted at build time by next/font rather than fetched from Google on
// every page load. One less external request, no render-blocking stylesheet,
// and nothing about a reader's visit leaves for a font.
const mono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
  display: "swap",
});

const sans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-sans",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Bellwether",
  description:
    "A model that notices it is getting worse, and replaces itself. Live status, " +
    "performance, and every decision it made about its own model.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${mono.variable} ${sans.variable}`}>
      <body>
        {/* Before the navigation, so a keyboard user does not tab through five
            links on every page to reach the thing they came for. */}
        <a href="#content" className="skip-link">
          Skip to content
        </a>
        <div className="shell">
          <Nav />
          <div id="content" className="content">
            {children}
          </div>
        </div>
      </body>
    </html>
  );
}
