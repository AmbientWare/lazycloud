import { useInfiniteQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { ChevronRight } from "lucide-react";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { RowsSkeleton } from "@/components/shared/RowsSkeleton";
import type { UsageCostRow } from "@/lib/api/schemas";
import { formatDuration } from "@/lib/format";
import { formatCostNanos } from "@/lib/money";
import { selectInfiniteList } from "@/lib/queries/infinite-list";
import {
  accountCostsQueryOptions,
  type UsageCostWindow,
  type UsageCostScope,
} from "@/lib/queries/usage";
import { CostComponents } from "./CostComponents";

export function UsageRows({
  window,
  scope,
  currency,
}: {
  window: UsageCostWindow;
  scope: UsageCostScope;
  currency: string;
}) {
  const costs = useInfiniteQuery(accountCostsQueryOptions(window, scope));
  const { items: rows, nextCursor } = selectInfiniteList(costs.data, costs.hasNextPage, rowKey);
  if (costs.isPending) return <RowsSkeleton rows={3} height="h-9" />;
  if (costs.isError && !costs.isFetchNextPageError)
    return <PanelError message={costs.error.message} />;
  if (!rows.length) return <PanelEmpty message="No usage in this period" className="py-4" />;
  return (
    <div className="content-transition min-w-0">
      <div
        aria-hidden="true"
        className="grid grid-cols-[minmax(0,1fr)_4.5rem_minmax(6rem,auto)] gap-3 border-b border-border/50 py-2 pl-9 pr-4 text-xs text-muted-foreground"
      >
        <span>{scope.groupBy === "task" ? "Run / resource" : "Workload"}</span>
        <span className="text-right">Runtime</span>
        <span className="text-right">Cost</span>
      </div>
      {rows.map((row) => (
        <details
          key={rowKey(row)}
          className="group/usage border-b border-border/40 last:border-b-0"
        >
          <summary className="grid cursor-pointer list-none grid-cols-[minmax(0,1fr)_4.5rem_minmax(6rem,auto)] items-center gap-3 py-2.5 pl-5 pr-4 text-xs outline-none hover:bg-muted/25 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
            <span className="flex min-w-0 items-center gap-1.5">
              <ChevronRight
                aria-hidden="true"
                className="size-3 shrink-0 text-muted-foreground transition-transform group-open/usage:rotate-90 motion-reduce:transition-none"
              />
              <span className="truncate">
                {row.workload_name ||
                  (row.task_id
                    ? `Run ${row.task_id.slice(0, 8)}`
                    : row.workload_id
                      ? "Workload removed"
                      : "Compute")}
              </span>
              {row.task_id && row.workload_name ? (
                <span className="mono shrink-0 text-muted-foreground">
                  {row.task_id.slice(0, 8)}
                </span>
              ) : null}
            </span>
            <span className="mono text-right tabular-nums text-muted-foreground">
              {runtime(row)}
            </span>
            <span className="mono text-right tabular-nums">
              {formatCostNanos(row.cost_nanos, currency)}
            </span>
          </summary>
          <div className="flex flex-wrap items-center justify-between gap-3 px-4 pb-3 pl-10">
            <CostComponents components={row.components} currency={currency} />
            {row.task_id && row.workspace_name ? (
              <Link
                className="interactive-link text-xs text-brand"
                to="/w/$workspace/tasks/$taskId"
                params={{ workspace: row.workspace_name, taskId: row.task_id }}
              >
                View run
              </Link>
            ) : null}
          </div>
        </details>
      ))}
      <InfiniteScrollBoundary
        nextCursor={nextCursor}
        loading={costs.isFetchingNextPage}
        error={costs.isFetchNextPageError}
        onLoadMore={() => void costs.fetchNextPage()}
        resourceLabel="usage rows"
      />
    </div>
  );
}

function runtime(row: UsageCostRow): string {
  const seconds = row.components.find((entry) => entry.component === "container_time")?.quantity;
  return seconds ? formatDuration(seconds * 1000) : "—";
}
function rowKey(row: UsageCostRow): string {
  return `${row.workspace_id}|${row.app_id}|${row.workload_id}|${row.task_id}`;
}
