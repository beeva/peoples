"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { deleteContacts, toast } from "@/lib/contacts";
import { useSelection } from "./Selection";

/** What you can do with the rows you have ticked. Hidden until you tick one.
 *
 *  Only deletion for now, and it asks first: the count is the whole of what
 *  makes a bulk delete different from a row one, so the confirm step spells it
 *  out rather than relying on the button label.
 */
export default function SelectionBar() {
  const router = useRouter();
  const [, startTransition] = useTransition();
  const { selected, count, total, setAll, clear } = useSelection();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  if (count === 0) return null;

  async function remove() {
    setBusy(true);
    try {
      const result = await deleteContacts(selected);
      toast(
        `Deleted ${result.contacts.toLocaleString()} ` +
          `contact${result.contacts === 1 ? "" : "s"}`,
      );
      setConfirming(false);
      clear();
      startTransition(() => router.refresh());
    } catch (err) {
      toast(`Error: ${err instanceof Error ? err.message : String(err)}`);
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="selection-bar" role="region" aria-label="Selected contacts">
      <span className="selection-count">
        <b>{count.toLocaleString()}</b> selected
      </span>

      {confirming ? (
        <>
          <span className="selection-warn">
            Deletes {count === 1 ? "this contact" : `these ${count} contacts`},
            the scraped records behind {count === 1 ? "it" : "them"}, and any
            chance of a later scrape collecting the same{" "}
            {count === 1 ? "person" : "people"} again. Cannot be undone.
          </span>
          <button
            type="button"
            className="selection-go danger"
            onClick={remove}
            disabled={busy}
          >
            {busy ? "Deleting…" : `Delete ${count.toLocaleString()}`}
          </button>
          <button
            type="button"
            className="selection-quiet"
            onClick={() => setConfirming(false)}
            disabled={busy}
          >
            Cancel
          </button>
        </>
      ) : (
        <>
          {count < total && (
            <button
              type="button"
              className="selection-quiet"
              onClick={() => setAll(true)}
            >
              Select all {total.toLocaleString()} on this page
            </button>
          )}
          <button
            type="button"
            className="selection-go"
            onClick={() => setConfirming(true)}
          >
            Delete selected
          </button>
          <button type="button" className="selection-quiet" onClick={clear}>
            Clear
          </button>
        </>
      )}
    </div>
  );
}
