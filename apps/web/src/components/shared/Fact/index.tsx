import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * One labelled reading in an instrument panel: a version, a port list, an
 * uptime.
 *
 * The value carries no size or colour of its own so the region it sits in sets
 * its density — a drawer summary and a compact inspector chip read the same
 * fact at different weights.
 *
 * `title` is the long form for a value the column is too narrow to show; a
 * string value is its own fallback, so only a value that reads differently in
 * full needs to pass one.
 */
export function Fact({
  label,
  value,
  mono = false,
  title,
}: {
  label: ReactNode;
  value: ReactNode;
  mono?: boolean;
  title?: string;
}) {
  return (
    <div className="min-w-0">
      <dt className="micro-label mb-1 truncate">{label}</dt>
      <dd
        className={cn("truncate tabular-nums", mono && "mono")}
        title={title ?? (typeof value === "string" ? value : undefined)}
      >
        {value}
      </dd>
    </div>
  );
}
