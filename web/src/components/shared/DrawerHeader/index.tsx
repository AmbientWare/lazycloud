import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * The bar every drawer opens with.
 *
 * `pr-12` leaves room for the sheet's own close control, which sits over this
 * bar rather than inside it — the one measurement a drawer header cannot be
 * written without, and the one a fourth hand-rolled copy of it loses.
 */
export function DrawerHeader({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <header className={cn("shrink-0 border-b border-border bg-card px-4 py-3 pr-12", className)}>
      {children}
    </header>
  );
}

/** The same bar at the same height, for a drawer whose record has not arrived. */
export function DrawerHeaderSkeleton({ children }: { children: ReactNode }) {
  return <DrawerHeader className="flex min-h-14 items-center gap-2.5">{children}</DrawerHeader>;
}
