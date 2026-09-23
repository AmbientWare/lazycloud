import { useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { RowsSkeleton } from "@/components/shared/RowsSkeleton";
import type { UsageCostRow } from "@/lib/api/schemas";
import { formatCostNanos } from "@/lib/money";
import { selectInfiniteList } from "@/lib/queries/infinite-list";
import {
  accountCostsQueryOptions,
  type UsageCostWindow,
  type UsageCostScope,
} from "@/lib/queries/usage";
import { CostComponents } from "./CostComponents";
import { UsageRows } from "./UsageRows";

export function UsageCostBreakdown({
  window,
  caption,
}: {
  window: UsageCostWindow;
  caption: string;
}) {
  const costs = useInfiniteQuery(accountCostsQueryOptions(window, { groupBy: "app" }));
  const { items: rows, nextCursor } = selectInfiniteList(costs.data, costs.hasNextPage, rowKey);
  const currency = costs.data?.pages[0]?.currency ?? "USD";
  if (costs.isPending) return <RowsSkeleton rows={5} height="h-10" />;
  if (costs.isError && !costs.isFetchNextPageError)
    return <PanelError message={costs.error.message} />;
  if (!rows.length) return <PanelEmpty message={`No usage during ${caption}`} className="h-32" />;
  const apps = rows.filter((row) => row.app_id && !row.category);
  const disks = rows.filter((row) => row.category === "disk");
  const builds = rows.filter((row) => row.category === "image-build");
  const unattributed = rows.filter((row) => !row.app_id && !row.category);
  return (
    <div className="content-transition min-h-0 lg:flex-1 lg:overflow-y-auto">
      {apps.map((row) => (
        <UsageGroup
          key={rowKey(row)}
          row={row}
          title={row.app_name || "App removed"}
          window={window}
          currency={currency}
          scope={{ groupBy: "workload", appId: row.app_id, workspaceId: row.workspace_id }}
        />
      ))}
      {(disks.length > 0 || builds.length > 0 || unattributed.length > 0) && (
        <h3 className="border-y border-border/70 bg-muted/15 px-4 py-2 text-xs text-muted-foreground">
          Other usage
        </h3>
      )}
      {unattributed.map((row) => (
        <UsageGroup
          key={rowKey(row)}
          row={row}
          title="Usage without an app"
          window={window}
          currency={currency}
          scope={{
            groupBy: "task",
            appId: "",
            workspaceId: row.workspace_id,
            category: "unattributed",
          }}
        />
      ))}
      {disks.map((row) => (
        <UsageGroup
          key={rowKey(row)}
          row={row}
          title={row.disk_name ? `Disk ${row.disk_name}` : "Disk removed"}
          window={window}
          currency={currency}
        />
      ))}
      {builds.map((row) => (
        <UsageGroup
          key={rowKey(row)}
          row={row}
          title="Image builds"
          window={window}
          currency={currency}
        />
      ))}
      <InfiniteScrollBoundary
        nextCursor={nextCursor}
        loading={costs.isFetchingNextPage}
        error={costs.isFetchNextPageError}
        onLoadMore={() => void costs.fetchNextPage()}
        resourceLabel="usage"
      />
    </div>
  );
}

function UsageGroup({
  row,
  title,
  window,
  currency,
  scope,
}: {
  row: UsageCostRow;
  title: string;
  window: UsageCostWindow;
  currency: string;
  scope?: UsageCostScope;
}) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="group/cost border-b border-border/60 last:border-b-0"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-sm outline-none hover:bg-muted/20 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
        <ChevronRight
          aria-hidden="true"
          className="size-3.5 shrink-0 text-muted-foreground transition-transform group-open/cost:rotate-90 motion-reduce:transition-none"
        />
        <span className="min-w-0 flex-1">
          <span className="font-medium">{title}</span>
          <span className="ml-3 inline-block text-xs text-muted-foreground">
            {row.workspace_name || "Workspace removed"}
          </span>
        </span>
        <span className="mono shrink-0 text-xs tabular-nums">
          {formatCostNanos(row.cost_nanos, currency)}
        </span>
      </summary>
      {open &&
        (scope ? (
          <div
            role="region"
            aria-label={`${title} details`}
            className="border-t border-border/50 bg-background/25"
          >
            <UsageRows window={window} scope={scope} currency={currency} />
          </div>
        ) : (
          <div className="px-4 pb-3 pl-10">
            <CostComponents components={row.components} currency={currency} />
          </div>
        ))}
    </details>
  );
}

function rowKey(row: UsageCostRow): string {
  return `${row.workspace_id}|${row.app_id}|${row.category}|${row.disk_id}`;
}
