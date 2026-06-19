import Link from "next/link";

function buildHref(params: Record<string, string>, page: number): string {
  const sp = new URLSearchParams(params);
  if (page <= 1) sp.delete("page");
  else sp.set("page", String(page));
  const qs = sp.toString();
  return qs ? `/?${qs}` : "/";
}

export default function Pagination({
  page,
  totalPages,
  baseParams,
}: {
  page: number;
  totalPages: number;
  baseParams: Record<string, string>;
}) {
  if (totalPages <= 1) return null;

  const nums: number[] = [];
  const lo = Math.max(2, page - 1);
  const hi = Math.min(totalPages - 1, page + 1);
  for (let p = lo; p <= hi; p++) nums.push(p);

  return (
    <nav className="pager" aria-label="Pagination">
      <Link
        className={`${page === 1 ? "disabled" : ""}`}
        href={buildHref(baseParams, page - 1)}
        aria-label="Previous page"
        scroll={false}
      >
        ‹
      </Link>

      <Link
        className={page === 1 ? "active" : ""}
        href={buildHref(baseParams, 1)}
        scroll={false}
      >
        1
      </Link>

      {lo > 2 && <span className="gap">…</span>}

      {nums.map((p) => (
        <Link
          key={p}
          className={p === page ? "active" : ""}
          href={buildHref(baseParams, p)}
          aria-current={p === page ? "page" : undefined}
          scroll={false}
        >
          {p}
        </Link>
      ))}

      {hi < totalPages - 1 && <span className="gap">…</span>}

      {totalPages > 1 && (
        <Link
          className={page === totalPages ? "active" : ""}
          href={buildHref(baseParams, totalPages)}
          scroll={false}
        >
          {totalPages}
        </Link>
      )}

      <Link
        className={`${page === totalPages ? "disabled" : ""}`}
        href={buildHref(baseParams, page + 1)}
        aria-label="Next page"
        scroll={false}
      >
        ›
      </Link>
    </nav>
  );
}
