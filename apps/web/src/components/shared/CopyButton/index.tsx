import { Check, Copy } from "lucide-react";

import { Button } from "@/components/ui/button";

import { useCopyToClipboard } from "./useCopyToClipboard";

/**
 * The icon control that puts a value on the clipboard.
 *
 * `label` names the thing being copied, not the action: the control writes the
 * verb, so every one of these reads the same way to a screen reader.
 */
export function CopyButton({
  value,
  label,
  variant = "ghost",
  disabled,
  className,
}: {
  value: string | (() => string);
  label: string;
  variant?: "ghost" | "outline";
  disabled?: boolean;
  className?: string;
}) {
  const { copied, copy } = useCopyToClipboard(value);

  return (
    <>
      <Button
        type="button"
        variant={variant}
        size="icon"
        disabled={disabled}
        onClick={copy}
        aria-label={`Copy ${label}`}
        title={copied ? "Copied" : `Copy ${label}`}
        className={className}
      >
        {copied ? <Check className="size-3.5 text-positive" /> : <Copy className="size-3.5" />}
      </Button>
      <span role="status" className="sr-only">
        {copied ? `Copied ${label}` : ""}
      </span>
    </>
  );
}
