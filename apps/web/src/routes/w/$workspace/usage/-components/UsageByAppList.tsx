import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { ArrowUpRight, ChevronRight, RefreshCw, TriangleAlert } from "lucide-react";

import { StubKindIcon } from "@/components/shared/StubKindIcon";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { UsageBillingAppSummary, UsageBillingAttribution } from "@/lib/api/schemas";
import { usageBillingWorkloadsQueryOptions, type UsageWindow } from "@/lib/queries/usage";
import { metricDisplay } from "@/lib/metric-display";
import { cn } from "@/lib/utils";
import { formatCostNanos } from "./usage-report";

export function UsageByAppList({
  apps,
  workspaceId,
  workspaceName,
  currency,
  window,
  bucketSeconds,
}: {
  apps: UsageBillingAppSummary[];
  workspaceId: string;
  workspaceName: string;
  currency: string;
  window: UsageWindow;
  bucketSeconds: number;
}) {
  const [openKey, setOpenKey] = useState<string | null>(null);

  if (!apps.length) {
    return (
      <div className="flex h-24 items-center justify-center text-sm text-muted-foreground">
        No app usage in this period
      </div>
    );
  }

  return (
    <div className="divide-y divide-border/70" aria-label="Usage by app">
      {apps.map((app) => {
        const key = app.app_id || "unlinked";
        return (
          <AppUsageSection
            key={key}
            app={app}
            open={openKey === key}
            onToggle={() => setOpenKey(openKey === key ? null : key)}
            workspaceId={workspaceId}
            workspaceName={workspaceName}
            currency={currency}
            window={window}
            bucketSeconds={bucketSeconds}
          />
        );
      })}
    </div>
  );
}

function AppUsageSection({
  app,
  open,
  onToggle,
  workspaceId,
  workspaceName,
  currency,
  window,
  bucketSeconds,
}: {
  app: UsageBillingAppSummary;
  open: boolean;
  onToggle: () => void;
  workspaceId: string;
  workspaceName: string;
  currency: string;
  window: UsageWindow;
  bucketSeconds: number;
}) {
  const workloads = useQuery({
    ...usageBillingWorkloadsQueryOptions(workspaceId, app.app_id, window, bucketSeconds),
    enabled: open,
  });
  const triggerId = `usage-app-${safeDomId(app.app_id || app.app_name)}`;
  const regionId = `${triggerId}-workloads`;

  return (
    <section>
      <button
        id={triggerId}
        type="button"
        aria-expanded={open}
        aria-controls={regionId}
        className="interactive-row group grid w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 px-4 py-3 text-left outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
        onClick={onToggle}
      >
        <ChevronRight
          className={cn(
            "interactive-row-indicator size-3.5 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-90 text-brand",
          )}
          aria-hidden="true"
        />
        <span className="min-w-0">
          <span className="block truncate text-sm font-medium text-foreground">
            {app.app_name || "Unlinked"}
          </span>
          <span className="mt-0.5 block text-[11px] text-muted-foreground">
            {workloads.data ? `${formatCount(workloads.data.data.length, "workload")} · ` : null}
            {formatCount(app.tasks, "task")}
          </span>
        </span>
        <span className="mono whitespace-nowrap text-sm font-medium tabular-nums text-foreground">
          {formatCostNanos(app.total_cost_nanos, false, currency)}
        </span>
      </button>
      {open ? (
        <div
          id={regionId}
          role="region"
          aria-labelledby={triggerId}
          className="border-t border-border/70 bg-background/30"
        >
          <div className="micro-label hidden grid-cols-[minmax(12rem,1.2fr)_minmax(14rem,1.8fr)_5rem_7rem_4rem] gap-3 border-b border-border/60 px-4 py-2 md:grid">
            <span>Workload</span>
            <span>Cost contributors</span>
            <span className="text-right">Tasks</span>
            <span className="text-right">Cost</span>
            <span className="sr-only">Inspect</span>
          </div>
          {workloads.isPending ? (
            <WorkloadsLoading />
          ) : workloads.isError ? (
            <WorkloadsFailure
              isRetrying={workloads.isFetching}
              onRetry={() => void workloads.refetch()}
            />
          ) : workloads.data.data.length ? (
            <div className="divide-y divide-border/60">
              {workloads.data.data.map((row) => (
                <WorkloadUsageRow
                  key={row.workload_id || `${row.workload_name}:unlinked`}
                  row={row}
                  workspaceName={workspaceName}
                  currency={currency}
                />
              ))}
            </div>
          ) : (
            <div className="flex h-20 items-center justify-center text-xs text-muted-foreground">
              No workload detail in this period
            </div>
          )}
        </div>
      ) : null}
    </section>
  );
}

function WorkloadsLoading() {
  return (
    <div className="space-y-3 px-4 py-3" aria-label="Loading workload usage">
      {[0, 1].map((item) => (
        <Skeleton key={item} className="h-11 w-full" />
      ))}
    </div>
  );
}

function WorkloadsFailure({ onRetry, isRetrying }: { onRetry: () => void; isRetrying: boolean }) {
  return (
    <div className="flex min-h-24 items-center justify-between gap-4 px-4 py-3" role="alert">
      <span className="flex min-w-0 items-center gap-2 text-xs text-destructive">
        <TriangleAlert className="size-4 shrink-0" />
        Workload usage could not be loaded
      </span>
      <Button variant="ghost" size="sm" onClick={onRetry} disabled={isRetrying}>
        <RefreshCw className={cn(isRetrying && "animate-spin")} />
        Retry
      </Button>
    </div>
  );
}

function WorkloadUsageRow({
  row,
  workspaceName,
  currency,
}: {
  row: UsageBillingAttribution;
  workspaceName: string;
  currency: string;
}) {
  const contributors = row.lines.filter((line) => line.cost_nanos > 0);
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-2 px-4 py-3 transition-colors hover:bg-accent/35 md:grid-cols-[minmax(12rem,1.2fr)_minmax(14rem,1.8fr)_5rem_7rem_4rem]">
      <div className="flex min-w-0 items-center gap-2">
        <StubKindIcon
          kind={row.workload_kind}
          className="size-3.5 shrink-0 text-muted-foreground"
        />
        <span className="min-w-0">
          <span className="block truncate text-sm font-medium">{row.workload_name}</span>
          {row.workload_kind ? (
            <span className="block truncate text-[11px] text-muted-foreground">
              {row.workload_kind}
            </span>
          ) : null}
        </span>
      </div>
      <div className="mono whitespace-nowrap text-right text-sm font-medium tabular-nums md:order-4">
        {formatCostNanos(row.total_cost_nanos, false, currency)}
      </div>
      <div className="order-3 col-span-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground md:order-none md:col-span-1">
        {contributors.length ? (
          contributors.map((line) => (
            <span key={line.metric} className="whitespace-nowrap">
              {metricDisplay(line.metric, line.unit).label}{" "}
              <span className="mono">{formatCostNanos(line.cost_nanos, false, currency)}</span>
            </span>
          ))
        ) : (
          <span>No attributed cost</span>
        )}
      </div>
      <div className="mono hidden text-right text-sm tabular-nums md:block">
        {Intl.NumberFormat().format(row.tasks)}
      </div>
      <div className="order-4 col-span-2 flex items-center justify-end gap-3 md:order-5 md:col-span-1">
        <span className="text-xs text-muted-foreground md:hidden">
          {formatCount(row.tasks, "task")}
        </span>
        {row.workload_id && row.app_id && row.workload_name ? (
          <Link
            to="/w/$workspace/apps/$appId/workloads/$name"
            params={{
              workspace: workspaceName,
              appId: row.app_id,
              name: row.workload_name,
            }}
            className="interactive-link inline-flex items-center gap-1 rounded-sm text-xs text-brand outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            View
            <ArrowUpRight className="size-3" />
          </Link>
        ) : (
          <span className="text-xs text-muted-foreground">Unlinked</span>
        )}
      </div>
    </div>
  );
}

function safeDomId(value: string): string {
  return value.replace(/[^a-zA-Z0-9_-]/g, "-");
}

function formatCount(value: number, singular: string): string {
  return `${Intl.NumberFormat().format(value)} ${value === 1 ? singular : `${singular}s`}`;
}
