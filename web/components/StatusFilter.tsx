"use client";

import { useEffect, useState, useTransition } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type { MessagedCounts, MessagedFilter, MessagedKey } from "@/lib/emails";

function fromActive(active: MessagedFilter): Set<MessagedKey> {
  return new Set<MessagedKey>(
    active === "all" ? [] : (active.split(",") as MessagedKey[]),
  );
}

export default function StatusFilter({
  active,
  counts,
}: {
  active: MessagedFilter;
  counts: MessagedCounts;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [, startTransition] = useTransition();

  // Local (optimistic) selection so a click toggles instantly, instead of
  // waiting for the server round-trip and re-rendering every box at once.
  const [selected, setSelected] = useState<Set<MessagedKey>>(() => fromActive(active));
  useEffect(() => {
    setSelected(fromActive(active));
  }, [active]);

  const allChecked = selected.size === 0;

  function navigate(sel: Set<MessagedKey>) {
    setSelected(sel);
    const params = new URLSearchParams(searchParams.toString());
    const keys = (["sent", "unsent"] as MessagedKey[]).filter((k) => sel.has(k));
    if (keys.length === 0) params.delete("messaged");
    else params.set("messaged", keys.join(","));
    params.delete("page"); // changing filter resets to page 1
    const qs = params.toString();
    startTransition(() => {
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    });
  }

  // "All" is exclusive: clicking it clears the specific selections.
  function toggleAll() {
    if (allChecked) return;
    navigate(new Set());
  }

  // Sent/Unsent are multi-select; clearing the last one falls back to All.
  function toggleKey(key: MessagedKey) {
    const next = new Set(selected);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    navigate(next);
  }

  return (
    <div className="status-filter" role="group" aria-label="Message status">
      <label className={`check-pill${allChecked ? " on" : ""}`}>
        <input type="checkbox" checked={allChecked} onChange={toggleAll} />
        <span>All</span>
        <span className="check-count">{counts.all.toLocaleString()}</span>
      </label>

      <label className={`check-pill sent${selected.has("sent") ? " on" : ""}`}>
        <input
          type="checkbox"
          checked={selected.has("sent")}
          onChange={() => toggleKey("sent")}
        />
        <span>Sent</span>
        <span className="check-count">{counts.sent.toLocaleString()}</span>
      </label>

      <label className={`check-pill${selected.has("unsent") ? " on" : ""}`}>
        <input
          type="checkbox"
          checked={selected.has("unsent")}
          onChange={() => toggleKey("unsent")}
        />
        <span>Unsent</span>
        <span className="check-count">{counts.unsent.toLocaleString()}</span>
      </label>
    </div>
  );
}
