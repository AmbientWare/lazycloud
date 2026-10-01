import { AlertTriangle, Loader2, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * The failure a region reports in place of the content it could not produce,
 * whether that came back from the API or was thrown while rendering.
 *
 * `error` is `unknown` because a render throw is not required to be an `Error`,
 * and a boundary that narrowed it would have to keep its own copy of this.
 */
export function ApiErrorNotice({
  error,
  title,
  onRetry,
  retrying = false,
  compact = false,
  className,
}: {
  error: unknown;
  title: string;
  onRetry?: () => void;
  retrying?: boolean;
  compact?: boolean;
  className?: string;
}) {
  const message =
    error instanceof Error && error.message ? error.message : "An unexpected error occurred.";

  return (
    <div
      role="alert"
      className={cn(
        "flex min-w-0 items-start gap-3 text-sm",
        compact ? "px-4 py-2.5" : "p-4",
        className,
      )}
    >
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className="font-medium text-foreground">{title}</p>
        <p className="mt-0.5 break-words text-muted-foreground">{message}</p>
      </div>
      {onRetry ? (
        <Button
          variant="outline"
          size="sm"
          disabled={retrying}
          aria-busy={retrying}
          onClick={onRetry}
        >
          {retrying ? <Loader2 className="animate-spin" /> : <RotateCcw />}
          {retrying ? "Retrying" : "Retry"}
        </Button>
      ) : null}
    </div>
  );
}
