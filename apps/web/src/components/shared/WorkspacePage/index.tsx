import type { ReactNode } from "react";

import { ContentTransition } from "@/components/shared/ContentTransition";
import { cn } from "@/lib/utils";

const PAGE_WIDTH = "max-w-[1600px]";

export function WorkspacePage({
  title,
  description,
  actions,
  headerDetails,
  children,
  contentClassName,
  pending = false,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  headerDetails?: ReactNode;
  children: ReactNode;
  contentClassName?: string;
  pending?: boolean;
}) {
  return (
    <div
      data-workspace-page=""
      className="view-enter flex h-full min-h-0 w-full flex-col overflow-hidden p-3 md:p-4"
    >
      <div className={cn("mx-auto flex min-h-0 w-full flex-1 flex-col gap-3", PAGE_WIDTH)}>
        <header className="panel shrink-0 rounded-md bg-card px-3 py-2.5">
          <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <h1 className="truncate text-xl font-semibold text-foreground">{title}</h1>
              {description !== undefined || pending ? (
                <div className="mt-1.5 min-h-4 text-xs text-muted-foreground">{description}</div>
              ) : null}
            </div>
            {actions ? (
              <div className="flex min-w-0 flex-wrap items-center justify-end gap-2">{actions}</div>
            ) : null}
          </div>
          {headerDetails ? (
            <div className="mt-3 border-t border-border/80 pt-3">{headerDetails}</div>
          ) : null}
        </header>
        <ContentTransition
          pending={pending}
          data-workspace-page-content=""
          className={cn("min-h-0 flex-1 overflow-hidden", contentClassName)}
        >
          {children}
        </ContentTransition>
      </div>
    </div>
  );
}
