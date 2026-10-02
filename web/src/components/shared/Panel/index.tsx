import type { ReactNode } from "react";

import { ContentTransition } from "@/components/shared/ContentTransition";
import { cn } from "@/lib/utils";

export function Panel({
  title,
  description,
  action,
  children,
  className,
  headerClassName,
  contentClassName,
  pending = false,
}: {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  headerClassName?: string;
  contentClassName?: string;
  pending?: boolean;
}) {
  return (
    <section
      data-panel=""
      className={cn("panel flex min-h-0 flex-col overflow-hidden rounded-md", className)}
    >
      <div
        className={cn(
          "flex min-h-10 shrink-0 items-center justify-between gap-3 border-b border-border/80 px-3 py-2",
          headerClassName,
        )}
      >
        <div className="min-w-0">
          <h2 className="truncate text-sm font-medium text-foreground">{title}</h2>
          {description ? (
            <p className="mt-0.5 truncate text-[11px] text-muted-foreground">{description}</p>
          ) : null}
        </div>
        {action}
      </div>
      <ContentTransition
        pending={pending}
        className={cn("min-h-0 flex-1 overflow-auto", contentClassName)}
        tabIndex={0}
      >
        {children}
      </ContentTransition>
    </section>
  );
}
