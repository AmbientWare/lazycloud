import { Check, Copy } from "lucide-react";

import { useCopyToClipboard } from "@/components/shared/CopyButton/useCopyToClipboard";
import { cn } from "@/lib/utils";

/**
 * An identifier shown inline, where the whole chip is the copy target — the id
 * text is the largest thing on screen worth pressing, so shrinking the target
 * to the icon beside it would be the wrong trade.
 */
export function CopyId({ value, className }: { value: string; className?: string }) {
  const { copied, copy } = useCopyToClipboard(value);

  return (
    <button
      type="button"
      onClick={copy}
      title={copied ? "Copied" : "Copy id"}
      className={cn(
        "group inline-flex max-w-full items-center gap-1.5 rounded-md px-1.5 py-0.5 text-xs text-muted-foreground outline-none transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring",
        className,
      )}
    >
      <span className="mono truncate">{value}</span>
      {copied ? (
        <Check className="size-3 shrink-0 text-positive" />
      ) : (
        <Copy className="size-3 shrink-0 opacity-0 transition-opacity group-hover:opacity-100" />
      )}
    </button>
  );
}
