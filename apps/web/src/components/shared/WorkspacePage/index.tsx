import type { CSSProperties, ReactNode } from "react";

import { cn } from "@/lib/utils";

export function WorkspacePage({
  title,
  description,
  actions,
  children,
  className,
  contentClassName,
  contentStyle,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  contentClassName?: string;
  contentStyle?: CSSProperties;
}) {
  return (
    <div
      data-workspace-page=""
      className={cn(
        "flex h-full min-h-0 w-full flex-col gap-4 overflow-hidden px-4 py-4 md:px-6 md:py-5",
        className,
      )}
    >
      <header className="flex shrink-0 flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="truncate text-xl font-semibold text-foreground">{title}</h1>
          {description ? (
            <div className="mt-1 text-xs text-muted-foreground">{description}</div>
          ) : null}
        </div>
        {actions ? (
          <div className="flex min-w-0 flex-wrap items-center gap-2">{actions}</div>
        ) : null}
      </header>
      <div
        data-workspace-page-content=""
        className={cn("min-h-0 flex-1 overflow-hidden", contentClassName)}
        style={contentStyle}
      >
        {children}
      </div>
    </div>
  );
}
