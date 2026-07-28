import { useEffect, useRef } from "react";
import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";

export function InfiniteScrollBoundary({
  nextCursor,
  loading,
  error,
  onLoadMore,
  resourceLabel,
}: {
  nextCursor: string | undefined;
  loading: boolean;
  error: boolean;
  onLoadMore: () => void;
  resourceLabel: string;
}) {
  const boundaryRef = useRef<HTMLDivElement>(null);
  const requestedCursorRef = useRef<string | undefined>(undefined);

  useEffect(() => {
    if (!nextCursor || loading || error) return;
    const boundary = boundaryRef.current;
    if (!boundary) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (!entry?.isIntersecting || requestedCursorRef.current === nextCursor) return;
        requestedCursorRef.current = nextCursor;
        onLoadMore();
      },
      { rootMargin: "0px 0px 160px" },
    );
    observer.observe(boundary);
    return () => observer.disconnect();
  }, [error, loading, nextCursor, onLoadMore]);

  if (!nextCursor) {
    return (
      <p className="sr-only" aria-live="polite">
        All {resourceLabel} loaded
      </p>
    );
  }

  return (
    <div
      ref={boundaryRef}
      className={
        loading || error
          ? "flex min-h-10 shrink-0 items-center justify-center px-3 py-2"
          : "h-px shrink-0"
      }
    >
      {error ? (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => {
            requestedCursorRef.current = undefined;
            onLoadMore();
          }}
        >
          Retry loading {resourceLabel}
        </Button>
      ) : loading ? (
        <p role="status" aria-live="polite" className="text-muted-foreground">
          <Loader2 className="size-4 animate-spin" aria-hidden="true" />
          <span className="sr-only">Loading more {resourceLabel}</span>
        </p>
      ) : (
        <span className="sr-only">More {resourceLabel} available</span>
      )}
    </div>
  );
}
