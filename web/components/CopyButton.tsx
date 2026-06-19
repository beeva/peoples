"use client";

import { useState } from "react";

export default function CopyButton({ value }: { value: string }) {
  const [done, setDone] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setDone(true);
      window.dispatchEvent(
        new CustomEvent("toast", { detail: `Copied ${value}` }),
      );
      setTimeout(() => setDone(false), 1200);
    } catch {
      window.dispatchEvent(new CustomEvent("toast", { detail: "Copy failed" }));
    }
  }

  return (
    <button
      className={`copy-btn${done ? " done" : ""}`}
      onClick={copy}
      title="Copy email"
      aria-label={`Copy ${value}`}
    >
      {done ? (
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={2.4}
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M20 6 9 17l-5-5" />
        </svg>
      ) : (
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <rect x="9" y="9" width="13" height="13" rx="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
      )}
    </button>
  );
}
