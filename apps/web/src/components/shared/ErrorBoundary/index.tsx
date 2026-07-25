import { Component, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useRouter, type ErrorComponentProps } from "@tanstack/react-router";
import { AlertTriangle, Loader2, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

function renderErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return "An unexpected rendering error occurred.";
}

function RenderErrorNotice({
  title,
  error,
  onRetry,
  retrying = false,
  compact = false,
  className,
}: {
  title: string;
  error: unknown;
  onRetry?: () => void;
  retrying?: boolean;
  compact?: boolean;
  className?: string;
}) {
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
        <p className="mt-0.5 break-words text-muted-foreground">{renderErrorMessage(error)}</p>
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

/**
 * Contains a render-time throw to one panel: the surrounding page, shell, and
 * sibling panels stay interactive while the failed region shows a retry
 * affordance. Give the boundary a `key` tied to the inspected resource so a
 * selection change discards a previous failure automatically.
 */
export class PanelErrorBoundary extends Component<
  { title: string; children: ReactNode; compact?: boolean; className?: string },
  { error: unknown; failed: boolean }
> {
  state: { error: unknown; failed: boolean } = { error: null, failed: false };

  static getDerivedStateFromError(error: unknown): { error: unknown; failed: boolean } {
    return { error, failed: true };
  }

  render(): ReactNode {
    if (this.state.failed) {
      return (
        <RenderErrorNotice
          title={this.props.title}
          error={this.state.error}
          compact={this.props.compact}
          className={this.props.className}
          onRetry={() => this.setState({ error: null, failed: false })}
        />
      );
    }
    return this.props.children;
  }
}

/**
 * Route-level `errorComponent`: replaces one route's content inside the shell
 * so navigation, breadcrumbs, and sibling surfaces stay alive. Retry resets
 * cached query state and router matches before re-rendering the route.
 */
export function RouteErrorFallback({ error, reset }: ErrorComponentProps) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [retrying, setRetrying] = useState(false);

  const retry = async () => {
    setRetrying(true);
    try {
      await queryClient.resetQueries();
      await router.invalidate();
    } finally {
      setRetrying(false);
      reset();
    }
  };

  return (
    <div className="flex h-full min-h-0 w-full items-center justify-center overflow-auto p-4">
      <section className="panel w-full max-w-md rounded-md">
        <RenderErrorNotice
          title="This page hit an unexpected error"
          error={error}
          retrying={retrying}
          onRetry={() => void retry()}
          className="p-5"
        />
      </section>
    </div>
  );
}
