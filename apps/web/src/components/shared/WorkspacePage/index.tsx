import type { CSSProperties, ReactNode } from "react";

import { cn } from "@/lib/utils";

/** The one measure every workspace page is set to. */
const PAGE_WIDTHS = {
  wide: "max-w-[1600px]",
  full: "",
} as const;

export type WorkspacePageWidth = keyof typeof PAGE_WIDTHS;

export function WorkspacePage({
  title,
  description,
  actions,
  children,
  width = "wide",
  className,
  contentClassName,
  contentStyle,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  width?: WorkspacePageWidth;
  className?: string;
  contentClassName?: string;
  contentStyle?: CSSProperties;
}) {
  return (
    <div
      data-workspace-page=""
      className={cn(
        "flex h-full min-h-0 w-full flex-col overflow-hidden px-4 py-4 md:px-6 md:py-5",
        className,
      )}
    >
      {/* Header and content share one measure, set here rather than passed in.
          Given to the content alone it centres under a full-bleed title, and on
          a wide display the heading sits some hundreds of pixels to the left of
          the thing it names. */}
      <div className={cn("mx-auto flex min-h-0 w-full flex-1 flex-col gap-4", PAGE_WIDTHS[width])}>
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
    </div>
  );
}
