import type { CSSProperties, ReactNode } from "react";

import { cn } from "@/lib/utils";

/** The one measure every workspace page is set to. */
const PAGE_WIDTHS = {
  wide: "max-w-[1600px]",
  full: "",
} as const;

export type WorkspacePageWidth = keyof typeof PAGE_WIDTHS;

/**
 * The frame every workspace page opens with.
 *
 * The header is a panel rather than bare text, and it is the only header any
 * page renders: name on the left, the facts that qualify it underneath, and
 * everything that acts on it on the right. A page that draws its own puts the
 * same title at a different weight in a different box, which is what makes
 * moving between two of them feel like moving between two products.
 *
 * Header and content share one measure, set here rather than passed in. Given to
 * the content alone it centres under a full-bleed title, and on a wide display
 * the heading sits some hundreds of pixels to the left of the thing it names.
 */
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
      <div className={cn("mx-auto flex min-h-0 w-full flex-1 flex-col gap-3", PAGE_WIDTHS[width])}>
        <header className="panel shrink-0 rounded-md bg-card px-4 py-3">
          <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <h1 className="truncate text-xl font-semibold text-foreground">{title}</h1>
              {description ? (
                <div className="mt-1.5 text-xs text-muted-foreground">{description}</div>
              ) : null}
            </div>
            {actions ? (
              <div className="flex min-w-0 flex-wrap items-center justify-end gap-2">{actions}</div>
            ) : null}
          </div>
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
