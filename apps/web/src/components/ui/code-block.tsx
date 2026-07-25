import type { ReactNode } from "react";

import { highlight } from "@/components/ui/code-syntax";
import { cn } from "@/lib/utils";

/* Code stays one shared primitive, with an explicit surface choice. Runtime
   output defaults to the ink sheet; editorial examples can sit on paper
   without relying on a parent theme override. */
export function CodeBlock({
  children,
  footer,
  className,
  bodyClassName,
  tone = "ink",
}: {
  children: string;
  footer?: ReactNode;
  className?: string;
  bodyClassName?: string;
  tone?: "ink" | "paper";
}) {
  return (
    <div
      className={cn(
        "min-w-0 overflow-hidden rounded-xl border border-border bg-card text-card-foreground",
        tone === "ink"
          ? "dark"
          : "code-block-paper shadow-[0_10px_24px_rgb(25_24_17/0.07)]",
        className,
      )}
    >
      <pre
        /* Bodies may scroll vertically when a fixed-height composition needs
           it, but code always wraps inside its panel instead of creating a
           second horizontal viewport. */
        className={cn(
          "m-0 overflow-x-hidden overflow-y-auto p-6 font-mono text-[11.5px] leading-[1.75] whitespace-pre-wrap text-muted-foreground [overflow-wrap:anywhere]",
          bodyClassName,
        )}
        data-marketing-code-surface=""
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
