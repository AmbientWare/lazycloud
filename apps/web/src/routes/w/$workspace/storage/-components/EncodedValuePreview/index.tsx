import { cn } from "@/lib/utils";

import { decodeEncodedValue } from "./decode";

export function EncodedValuePreview({
  valueBase64,
  emptyLabel = "No value",
  className,
}: {
  valueBase64: string | undefined;
  emptyLabel?: string;
  className?: string;
}) {
  const preview = decodeEncodedValue(valueBase64 ?? "");
  if (preview.kind === "empty") {
    return (
      <p className={cn("text-xs text-muted-foreground", className)}>
        {emptyLabel}
      </p>
    );
  }
  if (preview.kind === "binary") {
    return (
      <p className={cn("text-xs text-muted-foreground", className)}>
        Binary value, {preview.size.toLocaleString()} bytes
      </p>
    );
  }
  return (
    <pre
      className={cn(
        "mono max-h-48 overflow-auto whitespace-pre-wrap break-words text-xs text-foreground",
        className,
      )}
    >
      {preview.value}
    </pre>
  );
}
