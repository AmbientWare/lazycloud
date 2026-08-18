import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * Five readings need more room to stay legible than three do, so the wide
 * layouts hold two columns until `lg` while the narrow ones break at `sm`.
 */
const COLUMN_CLASSES = {
  2: "grid-cols-2",
  3: "grid-cols-2 sm:grid-cols-3",
  4: "grid-cols-2 sm:grid-cols-4",
  5: "grid-cols-2 lg:grid-cols-5",
} as const;

/**
 * The description list a row of `Fact`s is laid out in, and the owner of their
 * density: a `Fact` carries no size of its own, so the grid sets what its
 * readings are read at.
 */
export function FactGrid({
  columns,
  children,
  className,
}: {
  columns: keyof typeof COLUMN_CLASSES;
  children: ReactNode;
  className?: string;
}) {
  return (
    <dl className={cn("grid min-w-0 gap-x-6 gap-y-4 text-sm", COLUMN_CLASSES[columns], className)}>
      {children}
    </dl>
  );
}
