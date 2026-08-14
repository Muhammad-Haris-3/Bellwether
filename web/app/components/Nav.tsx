"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

// One navigation, on every page, in the same place.
//
// The old crumbs were re-declared in each page and in each of the queue's three
// render branches — so the cold-start view had none, the sign-in view had none,
// and a reader who landed on either had no way out. A single component cannot
// be forgotten in a branch.

const LINKS = [
  { href: "/", label: "Status", hint: "what has been collected" },
  { href: "/metrics", label: "Performance", hint: "how accurate it has been" },
  { href: "/timeline", label: "Timeline", hint: "every model decision" },
  { href: "/queue", label: "Queue", hint: "the live triage list" },
  { href: "/about", label: "About", hint: "what this is" },
] as const;

export function Nav() {
  const pathname = usePathname();

  return (
    <nav className="nav" aria-label="Primary">
      <Link href="/" className="nav-brand">
        <span className="nav-brand-mark" aria-hidden="true" />
        Bellwether
      </Link>

      <ul className="nav-links">
        {LINKS.map((link) => {
          const active = pathname === link.href;
          return (
            <li key={link.href}>
              <Link
                href={link.href}
                className={active ? "nav-link nav-link-active" : "nav-link"}
                aria-current={active ? "page" : undefined}
              >
                <span className="nav-link-label">{link.label}</span>
                <span className="nav-link-hint">{link.hint}</span>
              </Link>
            </li>
          );
        })}
      </ul>

      <p className="nav-foot">
        <a href="https://github.com/Muhammad-Haris-3/Bellwether">Source</a>
        <span className="nav-foot-note">
          Runs on free infrastructure. The API sleeps when idle.
        </span>
      </p>
    </nav>
  );
}
