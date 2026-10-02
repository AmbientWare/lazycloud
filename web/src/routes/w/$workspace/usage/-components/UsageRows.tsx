import { useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { ChevronRight } from "lucide-react";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { RowsSkeleton } from "@/components/shared/RowsSkeleton";
import type { Schemas } from "@/lib/api/client";
import { formatDuration, shortId } from "@/lib/format";
import { formatCostNanos } from "@/lib/money";
import {
  accountCostsQueryOptions,
  type UsageCostWindow,
  type UsageCostScope,
} from "@/lib/queries/usage";
import { CostComponents } from "./CostComponents";

type UsageCostRow = Schemas["UsageCostRow"];

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
  const pages = costs.data?.pages ?? [];
  const rows = pages.flatMap((page) => page.rows);
  const nextCursor = costs.hasNextPage ? pages.at(-1)?.next_cursor : undefined;
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
        <UsageRow key={rowKey(row)} row={row} window={window} scope={scope} currency={currency} />
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

function UsageRow({
  row,
  window,
  scope,
  currency,
}: {
  row: UsageCostRow;
  window: UsageCostWindow;
  scope: UsageCostScope;
  currency: string;
}) {
  const [open, setOpen] = useState(false);
  // A function's runs load when its row opens.
  const runs =
    open && scope.groupBy === "workload" && row.workload_id && row.workload_kind === "function";
  return (
    <details
      className="group/usage border-b border-border/40 last:border-b-0"
      onToggle={(event) => setOpen(event.currentTarget.open)}
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
                ? `Run ${shortId(row.task_id)}`
                : row.workload_id
                  ? "Workload removed"
                  : "Compute")}
          </span>
          {row.task_id && row.workload_name ? (
            <span className="mono shrink-0 text-muted-foreground">{shortId(row.task_id)}</span>
          ) : null}
        </span>
        <span className="mono text-right tabular-nums text-muted-foreground">{runtime(row)}</span>
        <span className="mono text-right tabular-nums">
          {formatCostNanos(row.cost_nanos, currency)}
        </span>
      </summary>
      <div className="flex flex-wrap items-center justify-between gap-3 px-4 pb-3 pl-10">
        <CostComponents components={row.components} currency={currency} />
        {row.task_id && row.workspace_name ? <ViewRun row={row} taskId={row.task_id} /> : null}
      </div>
      {runs ? (
        <UsageRuns
          window={window}
          scope={{
            groupBy: "task",
            appId: row.app_id,
            workspaceId: row.workspace_id,
            workloadId: row.workload_id,
          }}
          currency={currency}
        />
      ) : null}
    </details>
  );
}

/** One workload's runs with their share of its cost; idle time stays on the workload. */
function UsageRuns({
  window,
  scope,
  currency,
}: {
  window: UsageCostWindow;
  scope: UsageCostScope;
  currency: string;
}) {
  const costs = useInfiniteQuery(accountCostsQueryOptions(window, scope));
  const pages = costs.data?.pages ?? [];
  const rows = pages.flatMap((page) => page.rows).filter((row) => row.task_id);
  const nextCursor = costs.hasNextPage ? pages.at(-1)?.next_cursor : undefined;
  if (costs.isPending) return <RowsSkeleton rows={2} height="h-7" />;
  if (costs.isError && !costs.isFetchNextPageError)
    return <PanelError message={costs.error.message} />;
  if (!rows.length) return <PanelEmpty message="No runs in this period" className="py-3" />;
  return (
    <div className="min-w-0 pb-2 pl-10">
      {rows.map((row) => (
        <div
          key={rowKey(row)}
          className="grid grid-cols-[minmax(0,1fr)_4.5rem_minmax(6rem,auto)_4.5rem] items-center gap-3 py-1.5 pr-4 text-xs"
        >
          <span className="mono truncate text-muted-foreground">
            Run {row.task_id ? shortId(row.task_id) : ""}
          </span>
          <span className="mono text-right tabular-nums text-muted-foreground">{runtime(row)}</span>
          <span className="mono text-right tabular-nums">
            {formatCostNanos(row.cost_nanos, currency)}
          </span>
          <span className="text-right">
            {row.task_id && row.workspace_name ? <ViewRun row={row} taskId={row.task_id} /> : null}
          </span>
        </div>
      ))}
      <InfiniteScrollBoundary
        nextCursor={nextCursor}
        loading={costs.isFetchingNextPage}
        error={costs.isFetchNextPageError}
        onLoadMore={() => void costs.fetchNextPage()}
        resourceLabel="runs"
      />
    </div>
  );
}

function ViewRun({ row, taskId }: { row: UsageCostRow; taskId: string }) {
  return (
    <Link
      className="interactive-link text-xs text-brand"
      to="/w/$workspace/tasks/$taskId"
      params={{ workspace: row.workspace_name ?? "", taskId }}
    >
      View run
    </Link>
  );
}

function runtime(row: UsageCostRow): string {
  const seconds = row.components.find((entry) => entry.component === "container_time")?.quantity;
  return seconds ? formatDuration(seconds * 1000) : "—";
}
function rowKey(row: UsageCostRow): string {
  return [row.workspace_id, row.app_id, row.workload_id, row.task_id].join("|");
}
