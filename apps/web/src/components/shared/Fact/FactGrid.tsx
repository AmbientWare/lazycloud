import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * Two columns on a narrow display whatever the grid holds, because a reading
 * squeezed into a quarter of a phone's width truncates to its first word.
 */
const COLUMN_CLASSES = {
  2: "grid-cols-2",
  3: "grid-cols-2 sm:grid-cols-3",
  4: "grid-cols-2 sm:grid-cols-4",
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
