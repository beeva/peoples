/** Client-side calls that change contacts. Server components read through
 *  lib/emails.ts; this is the other direction, and runs in the browser. */

export type DeleteResult = {
  /** Merged contact rows removed. */
  contacts: number;
  /** Scraped records behind them, removed with them. */
  records: number;
  /** Scraper ids noted so the next scrape does not collect them again. */
  blocked: number;
};

/** Delete contacts by id. Throws with the server's reason if it refuses. */
export async function deleteContacts(ids: string[]): Promise<DeleteResult> {
  const res = await fetch("/api/contacts/delete", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ids }),
  });
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || "could not delete");
  return {
    contacts: Number(data.contacts) || 0,
    records: Number(data.records) || 0,
    blocked: Number(data.blocked) || 0,
  };
}

/** Show a message in the app's toaster (see components/Toaster.tsx). */
export function toast(message: string) {
  window.dispatchEvent(new CustomEvent("toast", { detail: message }));
}
