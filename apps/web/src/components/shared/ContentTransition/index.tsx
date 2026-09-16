import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

export function ContentTransition({
  pending = false,
  className,
  ...props
}: ComponentProps<"div"> & { pending?: boolean }) {
  return (
    <div
      {...props}
      aria-busy={pending}
      data-pending={pending}
      className={cn("content-transition", className)}
    />
  );
}
