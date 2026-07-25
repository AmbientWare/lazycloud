import { useState } from "react";
import { Check, Copy } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/** Beam-style copyable CLI command hint for empty states and onboarding. */
export function CliHint({ command, className }: { command: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard.writeText(command).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };
  return (
    <div
      className={cn(
        "flex items-center justify-between gap-2 rounded-md border border-border bg-muted/50 px-3 py-2",
        className,
      )}
    >
      <code className="mono truncate text-xs text-muted-foreground">$ {command}</code>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        onClick={copy}
        aria-label="Copy command"
        title="Copy command"
        className="size-7"
      >
        {copied ? <Check className="size-3.5 text-positive" /> : <Copy className="size-3.5" />}
      </Button>
    </div>
  );
}
