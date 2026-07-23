"use client";

import { useEffect, useState } from "react";

/** External links you have already opened, remembered by URL.
 *
 *  The browser's own :visited state is useless here -- it only lets us change a
 *  colour, and it does not survive a profile clean -- so the clicks are kept in
 *  localStorage and the link renders its own "seen" badge.
 */
const KEY = "visited-links";
const EVENT = "visited-links-change";
/** Plenty for a few thousand contacts, and keeps the entry from growing without
 *  bound; the oldest URLs fall off first. */
const MAX = 4000;

function readVisited(): string[] {
  try {
    const raw = window.localStorage.getItem(KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? (parsed as string[]) : [];
  } catch {
    return [];
  }
}

export default function VisitedLink({
  href,
  label,
  className = "meta-link",
}: {
  href: string;
  label: string;
  className?: string;
}) {
  // Starts false so the server markup and the first client paint agree; the
  // stored value is applied right after mount.
  const [visited, setVisited] = useState(false);

  useEffect(() => {
    const sync = () => setVisited(readVisited().includes(href));
    sync();
    // Other rows on this page (same URL) and other tabs both keep in step.
    window.addEventListener(EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, [href]);

  function mark() {
    setVisited(true);
    try {
      const next = readVisited().filter((u) => u !== href);
      next.push(href);
      window.localStorage.setItem(KEY, JSON.stringify(next.slice(-MAX)));
    } catch {
      // Private mode / full quota -- the badge still shows for this session.
    }
    window.dispatchEvent(new Event(EVENT));
  }

  return (
    <a
      className={className}
      data-visited={visited || undefined}
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title={visited ? `${href} (already opened)` : href}
      onClick={mark}
      onAuxClick={mark} // middle-click opens it too
    >
      {label}
      {visited && (
        <span className="visited-tick" aria-label="already opened">
          ✓
        </span>
      )}
    </a>
  );
}
