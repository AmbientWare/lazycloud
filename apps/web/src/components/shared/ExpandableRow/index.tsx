import { useId, useRef, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

export function ExpandableRow({
  open,
  onOpenChange,
  label,
  summary,
  children,
  className,
  buttonClassName,
  contentClassName,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  label: string;
  summary: ReactNode;
  children: ReactNode;
  className?: string;
  buttonClassName?: string;
  contentClassName?: string;
}) {
  const id = useId();
  const button = useRef<HTMLButtonElement>(null);
  return (
    <div className={className}>
      <button
        ref={button}
        id={`${id}-trigger`}
        type="button"
        aria-label={label}
        aria-expanded={open}
        aria-controls={`${id}-content`}
        data-selected={open}
        className={cn(
          "interactive-row group flex w-full min-w-0 items-center gap-3 px-3 py-3 text-left",
          buttonClassName,
        )}
        onClick={() => {
          onOpenChange(!open);
          if (!open)
            requestAnimationFrame(() => button.current?.scrollIntoView({ block: "nearest" }));
        }}
      >
        <ChevronRight
          aria-hidden="true"
          className={cn(
            "interactive-row-indicator size-3.5 shrink-0 text-muted-foreground",
            open && "rotate-90 text-brand",
          )}
        />
        {summary}
      </button>
      <div
        id={`${id}-content`}
        role="region"
        aria-labelledby={`${id}-trigger`}
        hidden={!open}
        className={cn("border-t border-border bg-background/35", contentClassName)}
      >
        {open ? children : null}
      </div>
    </div>
  );
}
