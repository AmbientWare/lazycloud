import type { ReactNode } from "react";

import { highlight } from "@/components/ui/code-syntax";
import { cn } from "@/lib/utils";

export function CodeBlock({
  children,
  footer,
  className,
  bodyClassName,
}: {
  children: string;
  footer?: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <div
      className={cn(
        "dark min-w-0 overflow-hidden rounded-xl border border-border bg-card text-card-foreground",
        className,
      )}
    >
      <pre
        className={cn(
          "m-0 overflow-x-hidden overflow-y-auto p-6 font-mono text-[11.5px] leading-[1.75] whitespace-pre-wrap text-muted-foreground [overflow-wrap:anywhere]",
          bodyClassName,
        )}
        tabIndex={0}
        role="region"
        aria-label="Code example"
      >
        <code>{highlight(children)}</code>
      </pre>
      {footer ? (
        <div className="min-h-11 border-t border-border px-3.5 font-mono text-[10px] text-muted-foreground">
          {footer}
        </div>
      ) : null}
    </div>
  );
}
