import React from "react";

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Wrap query-term matches in <mark>. Safe for both server and client. */
export function highlight(text: string, query: string): React.ReactNode {
  const value = text ?? "";
  const terms = (query || "")
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .map(escapeRegex);
  if (!terms.length) return value;

  const re = new RegExp(`(${terms.join("|")})`, "gi");
  // String.split with a capturing group keeps the matches at odd indices.
  const parts = value.split(re);
  return parts.map((part, i) =>
    i % 2 === 1 ? (
      <mark key={i}>{part}</mark>
    ) : (
      <React.Fragment key={i}>{part}</React.Fragment>
    ),
  );
}
