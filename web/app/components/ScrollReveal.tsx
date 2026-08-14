"use client";

import { useEffect, useRef, useState, ReactNode } from "react";

export function ScrollReveal({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [isVisible, setIsVisible] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || !("IntersectionObserver" in window)) {
      setIsVisible(true);
      return;
    }

    const observer = new IntersectionObserver(
      // Re-arms on exit rather than unobserving.
      //
      // The first version called unobserve() on the first intersection, so a
      // section animated once per page load and never again — scroll down and
      // back and everything was simply static. Tracking visibility both ways
      // means the entry plays whenever a section comes back into view.
      ([entry]) => {
        setIsVisible(entry.isIntersecting);
      },
      {
        threshold: 0.05,
        // Asymmetric: a section is considered visible slightly before it
        // reaches the bottom edge, and stays visible until well past the top.
        // Without the generous top margin a section re-hides while it is still
        // being read at the top of the viewport.
        rootMargin: "-40px 0px 200% 0px",
      }
    );

    const currentRef = ref.current;
    if (currentRef) {
      observer.observe(currentRef);
    }

    return () => {
      if (currentRef) {
        observer.unobserve(currentRef);
      }
    };
  }, []);

  return (
    <div
      ref={ref}
      className={`scroll-reveal ${isVisible ? "reveal-active" : ""} ${className}`}
    >
      {children}
    </div>
  );
}
