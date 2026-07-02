"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

/** Right-side slide-over shell used by the intercepted /contact/[id] route. */
export default function DetailDrawer({ children }: { children: React.ReactNode }) {
  const router = useRouter();

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") router.back();
    }
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [router]);

  return (
    <div
      className="drawer-overlay"
      onClick={(e) => {
        if (e.target === e.currentTarget) router.back();
      }}
    >
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="Contact detail">
        <div className="drawer-bar">
          <button className="drawer-close" onClick={() => router.back()} aria-label="Close">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 6 6 18M6 6l12 12" />
            </svg>
            Close
          </button>
        </div>
        <div className="drawer-body">{children}</div>
      </aside>
    </div>
  );
}
