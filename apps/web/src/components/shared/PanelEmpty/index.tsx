import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * The absence a region reports where its content would be.
 *
 * The fill — a fixed height, a `min-h-*` floor, `flex-1`, `h-full` — arrives
 * through `className` and stays the caller's decision: it is what holds a
 * region the size it was while loading, and one value would crush a result pane
 * and stretch a panel in the same stroke.
 *
 * With a `detail`, the message carries the weight and the detail stays muted,
 * so the two lines read in order rather than competing.
 */
export function PanelEmpty({
  message,
  detail,
  icon: Icon,
  className,
}: {
  message: ReactNode;
  detail?: ReactNode;
  icon?: LucideIcon;
  className?: string;
}) {
  return (
    <div
      className={cn("flex flex-col items-center justify-center gap-1 px-4 text-center", className)}
    >
      {Icon ? <Icon className="mb-2 size-5 text-muted-foreground" aria-hidden="true" /> : null}
      <p
        className={cn("text-sm", detail ? "font-medium text-foreground" : "text-muted-foreground")}
      >
        {message}
      </p>
      {detail ? <p className="max-w-sm text-xs leading-5 text-muted-foreground">{detail}</p> : null}
    </div>
  );
}
