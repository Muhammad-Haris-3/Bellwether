import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Bellwether",
  description:
    "A model that notices it is getting worse, and replaces itself. Live status.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
