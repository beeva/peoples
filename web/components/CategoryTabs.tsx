"use client";

import { useTransition } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type { SourceInfo, SourceKey } from "@/lib/emails";

export default function CategoryTabs({
  sources,
  active,
}: {
  sources: SourceInfo[];
  active: SourceKey;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [pending, startTransition] = useTransition();

  function select(key: SourceKey) {
    if (key === active) return;
    const params = new URLSearchParams(searchParams.toString());
    if (key === "all") params.delete("source");
    else params.set("source", key);
    params.delete("page"); // switching category resets to page 1
    const qs = params.toString();
    startTransition(() => {
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    });
  }

  return (
    <div className="tabs" role="tablist" aria-label="Data source">
      {sources.map((s) => (
        <button
          key={s.key}
          role="tab"
          aria-selected={s.key === active}
          className={`tab${s.key === active ? " active" : ""}`}
          data-source={s.key}
          onClick={() => select(s.key)}
          disabled={pending}
        >
          {s.label}
          <span className="tab-count">{s.count.toLocaleString()}</span>
        </button>
      ))}
    </div>
  );
}
