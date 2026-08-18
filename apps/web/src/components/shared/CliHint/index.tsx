import { CopyButton } from "@/components/shared/CopyButton";
import { cn } from "@/lib/utils";

/** A CLI command an empty state offers, ready to take to a terminal. */
export function CliHint({ command, className }: { command: string; className?: string }) {
  return (
    <div
      className={cn(
        "flex items-center justify-between gap-2 rounded-md border border-border bg-muted/50 px-3 py-2",
        className,
      )}
    >
      <code className="mono truncate text-xs text-muted-foreground">$ {command}</code>
      <CopyButton value={command} label="command" className="size-7" />
    </div>
  );
}
