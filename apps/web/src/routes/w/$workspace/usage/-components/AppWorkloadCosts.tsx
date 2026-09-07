import { useInfiniteQuery } from "@tanstack/react-query";

import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { RowsSkeleton } from "@/components/shared/RowsSkeleton";
import { StubKindIcon } from "@/components/shared/StubKindIcon";
import type { LedgerComponent, UsageCostRow } from "@/lib/api/schemas";
import { formatDuration, shareLabel } from "@/lib/format";
import { formatCostNanos } from "@/lib/money";
import { selectInfiniteList } from "@/lib/queries/infinite-list";
import { accountCostsQueryOptions, type UsageCostWindow } from "@/lib/queries/usage";

import { RowFigures } from "./RowFigures";

const COMPONENT_LABELS: Record<LedgerComponent, string> = {
  container_time: "Container",
  cpu: "CPU",
  memory: "Memory",
  gpu: "GPU",
  egress: "Egress",
  volume_storage: "Volume storage",
};

/**
 * What one app's workloads cost over the same range, and what each was charged
 * for.
 *
 * Stops at the workload rather than going on to tasks. A task carries an opaque
 * id and no name, so a third level would be a page of identifiers ranked by
 * fractions of a cent; what somebody who has opened an app actually wants next
 * is which resource the money went on, and that is on the row itself.
 *
 * Asked only once the row is open, so a list of fifty apps is one request rather
 * than fifty-one.
 */
export function AppWorkloadCosts({
  window,
  appId,
  appCostNanos,
  currency,
}: {
  window: UsageCostWindow;
  appId: string;
  appCostNanos: number;
  currency: string;
}) {
  const costs = useInfiniteQuery(accountCostsQueryOptions(window, { groupBy: "workload", appId }));
  const { items: rows, nextCursor } = selectInfiniteList(costs.data, costs.hasNextPage, rowKey);

  if (costs.isPending) {
    return <RowsSkeleton rows={3} height="h-8" className="px-4 py-3" />;
  }
  if (costs.isError && !costs.isFetchNextPageError) {
    return <PanelError message={costs.error.message} />;
  }
  if (rows.length === 0) {
    return (
      <PanelEmpty
        message="No workloads in this app were billed during this range"
        className="py-8"
      />
    );
  }

  return (
    <div className="flex min-h-0 flex-col">
      <ul className="divide-y divide-border/40">
        {rows.map((row) => {
          const share = appCostNanos > 0 ? row.cost_nanos / appCostNanos : 0;
          return (
            <li key={rowKey(row)} className="flex min-w-0 flex-col gap-1.5 py-2.5 pl-10 pr-3">
              <div className="flex min-w-0 items-center gap-3">
                <StubKindIcon kind={row.workload_kind} className="size-3.5 shrink-0" />
                <span
                  className="mono min-w-0 flex-1 truncate text-xs text-foreground"
                  title={row.workload_id}
                >
                  {row.workload_name ||
                    (row.category === "image-build"
                      ? "Image builds"
                      : row.workload_id
                        ? "Workload removed"
                        : "Unattributed")}
                </span>
                <span className="mono w-16 shrink-0 text-right text-[11px] tabular-nums text-muted-foreground">
                  {runtime(row)}
                </span>
                <RowFigures
                  share={share}
                  label={`${shareLabel(share)} of this app's spend`}
                  costNanos={row.cost_nanos}
                  currency={currency}
                  compact
                />
              </div>
              {/* What the row was charged for, straight from the ledger. A
                  component priced at zero is kept where something was measured —
                  egress at $0.00 says the traffic was counted and is free — and
                  dropped where nothing was: a GPU line on a workload that asked
                  for none is a resource named, not a resource measured. */}
              <ul className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground">
                {row.components
                  .filter((component) => component.quantity > 0)
                  .map((component) => (
                    <li key={component.component}>
                      {COMPONENT_LABELS[component.component]}{" "}
                      <span className="mono tabular-nums text-foreground">
                        {formatCostNanos(component.cost_nanos, currency)}
                      </span>
                    </li>
                  ))}
              </ul>
            </li>
          );
        })}
      </ul>
      <InfiniteScrollBoundary
        nextCursor={nextCursor}
        loading={costs.isFetchingNextPage}
        error={costs.isFetchNextPageError}
        onLoadMore={() => void costs.fetchNextPage()}
        resourceLabel="workloads"
      />
    </div>
  );
}

/** How long the workload's containers were held, which is what compute prices. */
function runtime(row: UsageCostRow): string {
  const seconds = row.components.find((entry) => entry.component === "container_time")?.quantity;
  return seconds ? formatDuration(seconds * 1_000) : "—";
}

/** Grouped by workload, the task id is empty on every row and the workspace and
 * app are the same on all of them, so the workload is what distinguishes one. */
function rowKey(row: UsageCostRow): string {
  return `${row.workspace_id}|${row.app_id}|${row.workload_id}`;
}
