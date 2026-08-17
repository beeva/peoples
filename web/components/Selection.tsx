"use client";

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

type Selection = {
  /** Selected ids, in the order the rows are on screen. */
  selected: string[];
  /** Rows on the page, selected or not. */
  total: number;
  count: number;
  has: (id: string) => boolean;
  toggle: (id: string) => void;
  /** Select or clear every row on the page at once. */
  setAll: (on: boolean) => void;
  clear: () => void;
};

const SelectionContext = createContext<Selection | null>(null);

export function useSelection(): Selection {
  const value = useContext(SelectionContext);
  if (!value) {
    throw new Error("Selection components must be inside <SelectionProvider>");
  }
  return value;
}

/** Row selection for the contact table, shared by the checkboxes and the
 *  "delete selected" bar above it.
 *
 *  The provider is a client component wrapped around server-rendered rows, so
 *  the table itself stays on the server and only the checkboxes and the bar
 *  ship as JavaScript.
 *
 *  A selection only ever means "these rows, here": changing page, filter or
 *  sort empties it. Carrying it across pages would let "delete 40 selected"
 *  remove people the user cannot see, which is not a thing to be casual about.
 */
export function SelectionProvider({
  ids,
  children,
}: {
  /** Ids of the rows currently on screen, in order. */
  ids: string[];
  children: React.ReactNode;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());

  // The rows themselves, as one value that can be compared: `ids` is a new
  // array on every render, so it cannot be a dependency of anything.
  const key = ids.join(" ");
  useEffect(() => {
    setSelected(new Set());
  }, [key]);

  const value = useMemo<Selection>(
    () => ({
      selected: ids.filter((id) => selected.has(id)),
      total: ids.length,
      count: selected.size,
      has: (id: string) => selected.has(id),
      toggle: (id: string) =>
        setSelected((prev) => {
          const next = new Set(prev);
          if (!next.delete(id)) next.add(id);
          return next;
        }),
      setAll: (on: boolean) => setSelected(on ? new Set(ids) : new Set()),
      clear: () => setSelected(new Set()),
    }),
    // `key` stands in for `ids`, which it is derived from.
    [selected, key], // eslint-disable-line react-hooks/exhaustive-deps
  );

  return (
    <SelectionContext.Provider value={value}>
      {children}
    </SelectionContext.Provider>
  );
}

/** The header checkbox: selects every row on the page, and shows a dash while
 *  only some of them are selected. */
export function SelectAll() {
  const { count, total, setAll } = useSelection();
  const ref = useRef<HTMLInputElement>(null);
  const all = total > 0 && count === total;
  // "Indeterminate" is not an attribute; it can only be set on the element.
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = count > 0 && !all;
  }, [count, all]);

  const label = all ? "Clear selection" : "Select every row on this page";
  return (
    <label className="row-select" title={label}>
      <input
        ref={ref}
        type="checkbox"
        checked={all}
        onChange={() => setAll(!all)}
        aria-label={label}
      />
    </label>
  );
}

/** One row's checkbox. */
export function RowSelect({ id, name }: { id: string; name: string }) {
  const { has, toggle } = useSelection();
  const on = has(id);
  const label = on ? `Deselect ${name}` : `Select ${name}`;
  return (
    <label className="row-select" title={label}>
      <input
        type="checkbox"
        checked={on}
        onChange={() => toggle(id)}
        aria-label={label}
      />
    </label>
  );
}
