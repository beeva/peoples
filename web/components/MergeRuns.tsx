"use client";

import { useMemo, useState, useTransition } from "react";
import { useRouter } from "next/navigation";

/** Fold one scrape run ("step") into another.
 *
 *  Two scrapes are often really one piece of work -- a run that died half way
 *  and the one that finished it -- and reading them as separate steps is just
 *  noise. Merging relabels the contacts of one run as belonging to the other;
 *  no contact is added, removed or changed otherwise.
 *
 *  The rewrite happens in the scraped data file on the server, so the choice is
 *  confirmed in place before it is sent.
 */
export default function MergeRuns({
  source,
  runs,
}: {
  source: string;
  runs: { run: number; count: number }[];
}) {
  const router = useRouter();
  const [, startTransition] = useTransition();
  const [open, setOpen] = useState(false);
  const [from, setFrom] = useState("");
  const [into, setInto] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const sorted = useMemo(
    () => [...runs].sort((a, b) => a.run - b.run),
    [runs],
  );

  // The common case is "the run I just did belongs with the one before it", so
  // that pair is pre-selected; either half can still be changed.
  const known = new Set(sorted.map((r) => String(r.run)));
  const fromValue =
    known.has(from) ? from : String(sorted[sorted.length - 1]?.run ?? "");
  const intoValue =
    known.has(into) && into !== fromValue
      ? into
      : String(
          (sorted.filter((r) => String(r.run) !== fromValue).pop() ?? sorted[0])
            ?.run ?? "",
        );

  const fromCount = sorted.find((r) => String(r.run) === fromValue)?.count ?? 0;
  const ready = fromValue !== "" && intoValue !== "" && fromValue !== intoValue;

  function close() {
    setOpen(false);
    setConfirming(false);
  }

  async function merge() {
    setBusy(true);
    try {
      const res = await fetch("/api/runs/merge", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          source,
          from: [Number(fromValue)],
          into: Number(intoValue),
        }),
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "could not merge");
      window.dispatchEvent(
        new CustomEvent("toast", {
          detail: `Merged run ${fromValue} into run ${intoValue} (${
            data.moved ?? 0
          } contacts)`,
        }),
      );
      // The merged-away run no longer exists, so drop the picks and let the
      // refreshed facet counts choose the defaults again.
      setFrom("");
      setInto("");
      close();
      startTransition(() => router.refresh());
    } catch (err) {
      window.dispatchEvent(
        new CustomEvent("toast", {
          detail: `Error: ${err instanceof Error ? err.message : String(err)}`,
        }),
      );
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  }

  if (sorted.length < 2) return null;

  if (!open) {
    return (
      <button
        type="button"
        className="facet-merge-open"
        onClick={() => setOpen(true)}
        title="Combine two steps into one"
      >
        Merge steps…
      </button>
    );
  }

  return (
    <div className="facet-merge">
      <span className="facet-merge-label">Merge run</span>
      <select
        className="facet-op"
        value={fromValue}
        disabled={busy}
        onChange={(e) => {
          setFrom(e.target.value);
          setConfirming(false);
        }}
        aria-label="Run to merge away"
      >
        {sorted.map((r) => (
          <option key={r.run} value={r.run}>
            Run {r.run} ({r.count.toLocaleString()})
          </option>
        ))}
      </select>
      <span className="facet-merge-label">into</span>
      <select
        className="facet-op"
        value={intoValue}
        disabled={busy}
        onChange={(e) => {
          setInto(e.target.value);
          setConfirming(false);
        }}
        aria-label="Run to keep"
      >
        {sorted
          .filter((r) => String(r.run) !== fromValue)
          .map((r) => (
            <option key={r.run} value={r.run}>
              Run {r.run} ({r.count.toLocaleString()})
            </option>
          ))}
      </select>

      {confirming ? (
        <>
          <span className="facet-merge-warn">
            Moves {fromCount.toLocaleString()} contacts into run {intoValue}. Run{" "}
            {fromValue} is gone afterwards.
          </span>
          <button
            type="button"
            className="facet-merge-go danger"
            onClick={merge}
            disabled={busy}
          >
            {busy ? "Merging…" : "Confirm"}
          </button>
        </>
      ) : (
        <button
          type="button"
          className="facet-merge-go"
          onClick={() => setConfirming(true)}
          disabled={!ready || busy}
        >
          Merge
        </button>
      )}
      <button
        type="button"
        className="facet-clear facet-merge-cancel"
        onClick={close}
        disabled={busy}
      >
        Cancel
      </button>
    </div>
  );
}
