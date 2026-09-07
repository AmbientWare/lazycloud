import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export function Panel({
  title,
  description,
  action,
  children,
  className,
  headerClassName,
  contentClassName,
}: {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  headerClassName?: string;
  contentClassName?: string;
}) {
  return (
    <section
      data-panel=""
      className={cn("panel flex min-h-0 flex-col overflow-hidden rounded-md", className)}
    >
      <div
        className={cn(
          "flex min-h-11 shrink-0 flex-wrap items-center justify-between gap-3 border-b border-border/80 px-4 py-2.5",
          headerClassName,
        )}
      >
        <div className="min-w-0">
          <h2 className="truncate text-sm font-medium text-foreground">{title}</h2>
          {description ? (
            <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>
          ) : null}
        </div>
        {action}
      </div>
      <div className={cn("min-h-0 flex-1 overflow-auto", contentClassName)} tabIndex={0}>
        {children}
      </div>
    </section>
  );
}
