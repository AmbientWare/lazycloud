import { Fragment, type ReactNode } from "react";

/**
 * The line under a page's name: what it holds, counted.
 *
 * One component so every page says it the same way — same separator, same
 * order of magnitude first, same silence while the counts are still loading.
 * A page that wrote its own would drift in the separator alone, and four
 * headers that differ only in punctuation read as four different products.
 *
 * Entries that are `null` are dropped rather than rendered empty, so a page
 * whose second figure has not arrived shows one fact instead of a dangling
 * separator.
 */
export function PageFacts({ items }: { items: (ReactNode | null)[] }) {
  const shown = items.filter((item) => item !== null && item !== undefined && item !== false);
  if (shown.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
      {shown.map((item, index) => (
        <Fragment key={index}>
          {index > 0 ? <span aria-hidden="true">·</span> : null}
          <span>{item}</span>
        </Fragment>
      ))}
    </div>
  );
}
