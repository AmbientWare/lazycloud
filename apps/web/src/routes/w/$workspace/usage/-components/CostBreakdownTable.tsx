import { useInfiniteQuery } from "@tanstack/react-query";

import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { StubKindIcon } from "@/components/shared/StubKindIcon";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { LedgerComponent, UsageCostGroupKey, UsageCostRow } from "@/lib/api/schemas";
import { formatDuration } from "@/lib/format";
import { formatCostNanos } from "@/lib/money";
import { selectInfiniteList } from "@/lib/queries/infinite-list";
import { usageCostsQueryOptions, type UsageCostWindow } from "@/lib/queries/usage";

const COMPONENT_LABELS: Record<string, string> = {
  container_time: "Container",
  cpu: "CPU",
  memory: "Memory",
  gpu: "GPU",
  egress: "Egress",
  volume_storage: "Volume storage",
};

const LEVEL_HEADING: Record<UsageCostGroupKey, string> = {
  app: "App",
  workload: "Workload",
  task: "Task",
};

/**
 * What each app, workload or task cost over the window, dearest first.
 *
 * Read straight from the priced ledger, so a row here and a line on the invoice
 * are the same segments summed twice rather than two calculations that have to
 * be kept in agreement.
 */
export function CostBreakdownTable({
  workspaceId,
  window,
  groupBy,
}: {
  workspaceId: string;
  window: UsageCostWindow;
  groupBy: UsageCostGroupKey;
}) {
  const costs = useInfiniteQuery(usageCostsQueryOptions(workspaceId, window, { groupBy }));
  const { items: rows, nextCursor } = selectInfiniteList(costs.data, costs.hasNextPage, rowKey);
  const currency = costs.data?.pages[0]?.currency ?? "USD";

  if (costs.isPending) {
    return (
      <div className="space-y-2 p-4" aria-hidden="true">
        {Array.from({ length: 5 }, (_, index) => (
          <Skeleton key={index} className="h-6 w-full" />
        ))}
      </div>
    );
  }

  if (rows.length === 0) {
    return (
      <div className="flex h-40 items-center justify-center px-6 text-center text-sm text-muted-foreground">
        Nothing ran in this period. Costs appear here as soon as a workload does.
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-auto">
      <Table className="min-w-[720px]">
        <TableHeader className="sticky top-0 z-10 bg-card">
          <TableRow className="border-b border-border hover:bg-transparent">
            <TableHead>{LEVEL_HEADING[groupBy]}</TableHead>
            <TableHead>Breakdown</TableHead>
            <TableHead className="text-right">Runtime</TableHead>
            <TableHead className="text-right">CPU</TableHead>
            <TableHead className="text-right">Memory</TableHead>
            <TableHead className="text-right">GPU</TableHead>
            <TableHead className="text-right">Cost</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => (
            <TableRow key={rowKey(row)}>
              <TableCell className="max-w-[240px]">
                <RowIdentity row={row} groupBy={groupBy} />
              </TableCell>
              <TableCell className="max-w-[260px]">
                <ul className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground">
                  {row.components.map((component) => (
                    <li key={component.component}>
                      {COMPONENT_LABELS[component.component] ?? component.component}{" "}
                      <span className="font-mono text-foreground">
                        {formatCostNanos(component.cost_nanos, currency)}
                      </span>
                    </li>
                  ))}
                </ul>
              </TableCell>
              <TableCell className="text-right font-mono text-xs">
                {formatDuration(quantityOf(row, "container_time") * 1_000)}
              </TableCell>
              <TableCell className="text-right font-mono text-xs text-muted-foreground">
                {seconds(quantityOf(row, "cpu"))}
              </TableCell>
              <TableCell className="text-right font-mono text-xs text-muted-foreground">
                {seconds(quantityOf(row, "memory"))}
              </TableCell>
              <TableCell className="text-right font-mono text-xs text-muted-foreground">
                {seconds(quantityOf(row, "gpu"))}
              </TableCell>
              <TableCell className="text-right font-mono text-sm">
                {formatCostNanos(row.cost_nanos, currency)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <InfiniteScrollBoundary
        nextCursor={nextCursor}
        loading={costs.isFetchingNextPage}
        error={costs.isError}
        onLoadMore={() => void costs.fetchNextPage()}
        resourceLabel="cost rows"
      />
    </div>
  );
}

function RowIdentity({ row, groupBy }: { row: UsageCostRow; groupBy: UsageCostGroupKey }) {
  if (groupBy === "task") {
    return (
      <span className="block truncate font-mono text-xs" title={row.task_id}>
        {row.task_id || "Unattributed"}
      </span>
    );
  }
  if (groupBy === "workload") {
    return (
      <span className="flex min-w-0 items-center gap-2">
        <StubKindIcon kind={row.workload_kind} className="size-3 shrink-0" />
        <span className="min-w-0 truncate text-sm" title={row.workload_id}>
          {row.workload_name || row.workload_id || "Unattributed"}
        </span>
      </span>
    );
  }
  return (
    <span className="block truncate text-sm" title={row.app_id}>
      {row.app_name || row.app_id || "Unattributed"}
    </span>
  );
}

/**
 * The whole identity a row carries, which is what distinguishes it at every
 * level.
 *
 * The deepest id alone does not: a container that is not a task carries an empty
 * `task_id`, and every row grouped by app carries an empty workload and task id.
 * Keying on that would collapse every such row into the first one, both in the
 * dedupe `selectInfiniteList` applies across pages and in React's reconciliation.
 */
function rowKey(row: UsageCostRow): string {
  return `${row.app_id}|${row.workload_id}|${row.task_id}`;
}

/**
 * What a row was charged for of one resource, in the unit its rate is published
 * per. Absent means the row used none of it, which reads as nothing used.
 */
function quantityOf(row: UsageCostRow, component: LedgerComponent): number {
  return row.components.find((entry) => entry.component === component)?.quantity ?? 0;
}

/** Resource-seconds, which get large fast and read better as whole numbers. */
function seconds(value: number): string {
  if (value === 0) return "—";
  return value >= 10 ? Intl.NumberFormat().format(Math.round(value)) : value.toFixed(2);
}
