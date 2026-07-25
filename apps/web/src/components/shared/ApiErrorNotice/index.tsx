import { useEffect, useState } from "react";
import { AlertTriangle, Loader2, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api/client";
import { cn } from "@/lib/utils";

export function ApiErrorNotice({
  error,
  title,
  onRetry,
  retrying = false,
  compact = false,
  className,
}: {
  error: Error;
  title: string;
  onRetry?: () => void;
  retrying?: boolean;
  compact?: boolean;
  className?: string;
}) {
  const apiError = error instanceof ApiError ? error : null;
  const [now, setNow] = useState(() => Date.now());
  const waitSeconds = apiError?.retryAt ? Math.max(0, Math.ceil((apiError.retryAt - now) / 1_000)) : 0;

  useEffect(() => {
    if (!apiError?.retryAt || waitSeconds === 0) return;
    const timer = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(timer);
  }, [apiError?.retryAt, waitSeconds]);

  return (
    <div
      role="alert"
      className={cn("flex min-w-0 items-start gap-3 text-sm", compact ? "px-4 py-2.5" : "p-4", className)}
    >
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className="font-medium text-foreground">{title}</p>
        <p className="mt-0.5 break-words text-muted-foreground">{error.message}</p>
        {apiError?.requestId || apiError?.status === 429 ? (
          <div className="mt-1.5 flex min-w-0 flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
            {apiError.status === 429 ? (
              <span>{waitSeconds > 0 ? `Retry available in ${waitSeconds}s` : "Retry available"}</span>
            ) : null}
            {apiError.requestId ? (
              <span className="mono min-w-0 truncate" title={apiError.requestId}>
                Request {apiError.requestId}
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
      {onRetry ? (
        <Button
          variant="outline"
          size="sm"
          disabled={retrying || waitSeconds > 0}
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
