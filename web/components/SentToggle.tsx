"use client";

import { useEffect, useState, useTransition } from "react";
import { useRouter } from "next/navigation";

/**
 * Checkbox that marks a contact as emailed (or not) by hand, for messages sent
 * outside this app. Unchecking forgets the send history for every one of the
 * contact's addresses.
 */
export default function SentToggle({
  id,
  name,
  sent,
  variant = "bare",
}: {
  id: string;
  name: string;
  sent: boolean;
  /** "bare" is the table's first-column checkbox; "labeled" adds a caption. */
  variant?: "bare" | "labeled";
}) {
  const router = useRouter();
  const [, startTransition] = useTransition();
  // Optimistic: flip immediately, then reconcile with the server's answer.
  const [checked, setChecked] = useState(sent);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    setChecked(sent);
  }, [sent]);

  async function toggle() {
    const next = !checked;
    setChecked(next);
    setSaving(true);
    try {
      const res = await fetch("/api/message/mark", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ id, sent: next }),
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "could not update status");
      setChecked(Boolean(data.messaged));
      window.dispatchEvent(
        new CustomEvent("toast", {
          detail: next ? `Marked ${name} as sent` : `Marked ${name} as unsent`,
        }),
      );
      // Refresh the server components so the badge, row tint and the
      // sent/unsent counts all agree with the new state.
      startTransition(() => router.refresh());
    } catch (err) {
      setChecked(!next);
      window.dispatchEvent(
        new CustomEvent("toast", {
          detail: `Error: ${err instanceof Error ? err.message : String(err)}`,
        }),
      );
    } finally {
      setSaving(false);
    }
  }

  const label = checked ? `Mark ${name} as unsent` : `Mark ${name} as sent`;

  return (
    <label className={`sent-toggle${variant === "labeled" ? " labeled" : ""}`} title={label}>
      <input
        type="checkbox"
        checked={checked}
        disabled={saving}
        onChange={toggle}
        aria-label={label}
      />
      {variant === "labeled" && <span>{checked ? "Sent" : "Mark as sent"}</span>}
    </label>
  );
}
