import type * as React from "react";

import { cn } from "@/lib/utils";

type BadgeTone = "success" | "warning" | "danger" | "muted";

const tones: Record<BadgeTone, string> = {
  success: "border-positive/30 bg-positive/15 text-positive",
  warning: "border-warning/30 bg-warning/15 text-warning",
  danger: "border-destructive/30 bg-destructive/15 text-destructive",
  muted: "border-input bg-transparent text-muted-foreground",
};

export function Badge({
  className,
  tone,
  ...props
}: React.HTMLAttributes<HTMLSpanElement> & { tone: BadgeTone }) {
  return (
    <span
      className={cn(
        "inline-flex h-5 items-center rounded border px-1.5 text-[11px] font-medium",
        tones[tone],
        className,
      )}
      {...props}
    />
  );
}
