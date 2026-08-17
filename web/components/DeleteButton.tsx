"use client";

import { useEffect, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { deleteContacts, toast } from "@/lib/contacts";

/** Delete one contact, from its row in the table.
 *
 *  Deletion cannot be undone -- it takes the contact, the scraped records
 *  behind it and, so the next scrape does not simply collect the same person
 *  again, a note of their scraper id. So the button arms on the first click and
 *  deletes on the second, and disarms itself a few seconds later if the second
 *  never comes: a single misclick in a dense table should never cost a record.
 */
export default function DeleteButton({
  id,
  name,
}: {
  id: string;
  name: string;
}) {
  const router = useRouter();
  const [, startTransition] = useTransition();
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!armed) return;
    const t = window.setTimeout(() => setArmed(false), 4000);
    return () => window.clearTimeout(t);
  }, [armed]);

  async function click() {
    if (!armed) {
      setArmed(true);
      return;
    }
    setArmed(false);
    setBusy(true);
    try {
      const result = await deleteContacts([id]);
      toast(
        `Deleted ${name}` +
          (result.records > 1 ? ` and ${result.records} records` : ""),
      );
      // The row is gone; re-render the list, counts and facets without it.
      startTransition(() => router.refresh());
    } catch (err) {
      toast(`Error: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(false);
    }
  }

  const label = armed ? `Delete ${name} — click again` : `Delete ${name}`;
  return (
    <button
      type="button"
      className={`icon-link danger${armed ? " armed" : ""}`}
      onClick={click}
      disabled={busy}
      title={label}
      aria-label={label}
    >
      {armed ? (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.2}
             strokeLinecap="round" strokeLinejoin="round">
          <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
          <path d="M12 9v4M12 17h.01" />
        </svg>
      ) : (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
             strokeLinecap="round" strokeLinejoin="round">
          <path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6" />
          <path d="M10 11v6M14 11v6" />
        </svg>
      )}
    </button>
  );
}
