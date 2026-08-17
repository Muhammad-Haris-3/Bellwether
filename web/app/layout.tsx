import type { Metadata } from "next";
import { Archivo } from "next/font/google";
import "./globals.css";

// One family for the whole site — headings, body, and figures. Archivo has the
// tabular figures the tables need, so nothing here has to fall back to a
// separate monospace for numbers.
const archivo = Archivo({
  subsets: ["latin"],
  weight: ["400", "500", "600", "800"],
  style: ["normal", "italic"],
  variable: "--font-archivo",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Bellwether",
  description:
    "A model that notices it is getting worse, and replaces itself. Live status.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={archivo.variable}>
      <body>{children}</body>
    </html>
  );
}
