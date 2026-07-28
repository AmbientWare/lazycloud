import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { Download, LoaderCircle, RefreshCw, TriangleAlert } from "lucide-react";
import { toast } from "sonner";
import { z } from "zod";

import { PanelErrorBoundary, RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { Panel } from "@/components/shared/Panel";
import { LinearSelect, LinearSelectItem } from "@/components/shared/LinearSelect";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { ConcurrencyLimitList, UsageBillingOverview } from "@/lib/api/schemas";
import { concurrencyLimitsQueryOptions } from "@/lib/queries/concurrency";
import {
  downloadUsageBillingCsv,
  usageBillingOverviewQueryOptions,
  type UsageWindow,
  type UsageWindowSelection,
} from "@/lib/queries/usage";
import { useWorkspace } from "@/lib/workspace-context";
import { cn } from "@/lib/utils";
import { CostActivityChart } from "./-components/CostActivityChart";
import { UsageByAppList } from "./-components/UsageByAppList";
import { billingLabel, formatCostNanos, formatUsageQuantity } from "./-components/usage-report";

const usageRanges = ["current", "24h", "7d", "30d"] as const;
type UsageRange = (typeof usageRanges)[number];

const rangeHours = { "24h": 24, "7d": 168, "30d": 720 } as const;
const rangeBucketSeconds: Record<UsageRange, number> = {
  current: 86_400,
  "24h": 3_600,
  "7d": 21_600,
  "30d": 86_400,
};

const searchSchema = z.object({
  range: z.enum(usageRanges).optional(),
});

export const Route = createFileRoute("/w/$workspace/usage/")({
  validateSearch: searchSchema,
  loaderDeps: ({ search }) => ({ range: search.range ?? "24h" }),
  loader: ({ deps }) => createUsageWindowSelection(deps.range),
  component: UsagePage,
  errorComponent: RouteErrorFallback,
});

function createUsageWindowSelection(range: UsageRange): UsageWindowSelection {
  if (range === "current") return { period: "current" };
  const end = new Date();
  const start = new Date(end.getTime() - rangeHours[range] * 3_600_000);
  return { start: start.toISOString(), end: end.toISOString() };
}

function UsagePage() {
  const { range = "24h" } = Route.useSearch();
  const initialWindow = Route.useLoaderData();
  return (
    <UsagePageContent
      key={`${range}:${usageWindowIdentity(initialWindow)}`}
      range={range}
      initialWindow={initialWindow}
    />
  );
}

function UsagePageContent({
  range,
  initialWindow,
}: {
  range: UsageRange;
  initialWindow: UsageWindowSelection;
}) {
  const { workspace } = useWorkspace();
  const usageWindow = useTrailingUsageWindow(range, initialWindow);
  const [isExporting, setIsExporting] = useState(false);
  const billing = useQuery(
    usageBillingOverviewQueryOptions(workspace.id, usageWindow, rangeBucketSeconds[range]),
  );
  const concurrency = useQuery(concurrencyLimitsQueryOptions(workspace.id));

  return (
    <WorkspacePage
      title="Usage"
      description={
        billing.data
          ? formatUsageWindow(billing.data)
          : "period" in usageWindow
            ? "Current billing period"
            : formatUsageWindow(usageWindow)
      }
      actions={
        <>
          <RangeSelector workspaceName={workspace.name} active={range} />
          <Button
            variant="outline"
            size="sm"
            disabled={!billing.data || isExporting}
            onClick={async () => {
              if (!billing.data || isExporting) return;
              setIsExporting(true);
              try {
                const report = await downloadUsageBillingCsv(
                  workspace.id,
                  { start: billing.data.start, end: billing.data.end },
                  rangeBucketSeconds[range],
                );
                downloadBlob(report.filename, report.blob);
              } catch {
                toast.error("Usage export failed", {
                  description: "The billing report could not be downloaded. Try again shortly.",
                });
              } finally {
                setIsExporting(false);
              }
            }}
          >
            {isExporting ? <LoaderCircle className="animate-spin" /> : <Download />}
            {isExporting ? "Exporting" : "Export CSV"}
          </Button>
        </>
      }
      contentClassName="overflow-y-auto pb-1 lg:overflow-hidden"
    >
      {billing.isPending ? (
        <UsageLoading />
      ) : billing.isError ? (
        <LoadFailure onRetry={() => void billing.refetch()} isRetrying={billing.isFetching} />
      ) : (
        <div className="grid min-h-full gap-4 lg:h-full lg:grid-cols-12 lg:grid-rows-[minmax(15rem,0.75fr)_minmax(22rem,1.25fr)]">
          <Panel
            title="Cost summary"
            description="Cost and resource consumption"
            className="min-h-[15rem] lg:col-span-4 lg:min-h-0"
            contentClassName="overflow-y-auto"
          >
            <SpendSummary
              report={billing.data}
              concurrency={concurrency.data}
              concurrencyError={concurrency.isError}
            />
          </Panel>

          <Panel
            title="Cost activity"
            description="Cost by interval"
            className="min-h-[16rem] lg:col-span-8 lg:min-h-0"
            contentClassName="overflow-hidden"
          >
            <PanelErrorBoundary title="Cost activity could not be displayed">
              <CostActivityChart report={billing.data} />
            </PanelErrorBoundary>
          </Panel>

          <Panel title="Apps" className="min-h-[22rem] lg:col-span-12 lg:min-h-0">
            <UsageByAppList
              apps={billing.data.apps}
              workspaceId={workspace.id}
              workspaceName={workspace.name}
              currency={billing.data.currency}
              window={{ start: billing.data.start, end: billing.data.end }}
              bucketSeconds={rangeBucketSeconds[range]}
            />
          </Panel>
        </div>
      )}
    </WorkspacePage>
  );
}

function useTrailingUsageWindow(
  range: UsageRange,
  initialWindow: UsageWindowSelection,
): UsageWindowSelection {
  const [usageWindow, setUsageWindow] = useState(initialWindow);

  useEffect(() => {
    if (range === "current") return;
    const interval = globalThis.setInterval(() => {
      setUsageWindow(createUsageWindowSelection(range));
    }, 60_000);
    return () => globalThis.clearInterval(interval);
  }, [range]);

  return usageWindow;
}

function usageWindowIdentity(window: UsageWindowSelection): string {
  return "period" in window ? window.period : window.end;
}

function SpendSummary({
  report,
  concurrency,
  concurrencyError,
}: {
  report: UsageBillingOverview;
  concurrency: ConcurrencyLimitList | undefined;
  concurrencyError: boolean;
}) {
  const lines = new Map(report.summary.map((line) => [line.metric, line]));
  const capacity = useMemo(() => {
    const limits = concurrency?.limits ?? [];
    return {
      used: limits.reduce((total, limit) => total + limit.in_flight, 0),
      limit: limits.reduce((total, item) => total + item.limit, 0),
      saturated: limits.filter((limit) => limit.saturated).length,
    };
  }, [concurrency?.limits]);
  const resourceReadouts = ["cpu_seconds", "memory_gib_seconds", "gpu_seconds"] as const;
  const tasks = report.apps.reduce((total, app) => total + app.tasks, 0);

  return (
    <div className="flex h-full min-h-0 flex-col px-4 py-3">
      <div className="border-b border-border/70 pb-3">
        <div className="min-w-0">
          <div className="micro-label mb-1">{billingLabel(report)}</div>
          <div className="readout text-2xl">
            {formatCostNanos(report.total_cost_nanos, false, report.currency)}
          </div>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-x-5 gap-y-3 py-3">
        {resourceReadouts.map((metric) => {
          const line = lines.get(metric);
          return (
            <UsageReadout
              key={metric}
              label={readoutLabel(metric)}
              value={line ? formatUsageQuantity(line) : "0"}
            />
          );
        })}
        <UsageReadout label="Tasks" value={Intl.NumberFormat().format(tasks)} />
      </div>
      {concurrencyError ? (
        <div className="mt-auto border-t border-border/70 pt-3 text-xs text-destructive">
          Concurrency unavailable
        </div>
      ) : capacity.limit > 0 ? (
        <div className="mt-3 flex items-center gap-3 border-t border-border/70 pt-3">
          <div className="h-1.5 flex-1 overflow-hidden bg-muted">
            <div
              className={cn("h-full bg-brand", capacity.saturated && "bg-warning")}
              style={{ width: `${Math.min((capacity.used / capacity.limit) * 100, 100)}%` }}
            />
          </div>
          {capacity.saturated ? (
            <span className="whitespace-nowrap text-xs text-warning">
              Concurrency {capacity.used} / {capacity.limit}
            </span>
          ) : (
            <span className="whitespace-nowrap text-xs text-muted-foreground">
              Concurrency {capacity.used} / {capacity.limit}
            </span>
          )}
        </div>
      ) : null}
    </div>
  );
}

function UsageReadout({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="micro-label mb-1 truncate">{label}</div>
      <div className="mono truncate text-sm tabular-nums">{value}</div>
    </div>
  );
}

function readoutLabel(metric: string): string {
  if (metric === "cpu_seconds") return "CPU";
  if (metric === "memory_gib_seconds") return "Memory";
  return "GPU";
}

function RangeSelector({ workspaceName, active }: { workspaceName: string; active: UsageRange }) {
  return (
    <LinearSelect ariaLabel="Usage period" className="max-w-full" listClassName="flex-none">
      {usageRanges.map((option) => (
        <LinearSelectItem key={option} selected={active === option}>
          <Link
            to="/w/$workspace/usage"
            params={{ workspace: workspaceName }}
            search={option === "24h" ? {} : { range: option }}
          >
            {option === "current" ? "Current period" : option}
          </Link>
        </LinearSelectItem>
      ))}
    </LinearSelect>
  );
}

function UsageLoading() {
  return (
    <div className="grid min-h-full gap-4 lg:h-full lg:grid-cols-12 lg:grid-rows-[minmax(15rem,0.75fr)_minmax(22rem,1.25fr)]">
      <Panel title="Cost summary" className="min-h-[15rem] lg:col-span-4 lg:min-h-0">
        <SummarySkeleton />
      </Panel>
      <Panel title="Cost activity" className="min-h-[16rem] lg:col-span-8 lg:min-h-0">
        <Skeleton className="m-4 h-48" />
      </Panel>
      <Panel title="Apps" className="min-h-[22rem] lg:col-span-12 lg:min-h-0">
        <TableSkeleton />
      </Panel>
    </div>
  );
}

function SummarySkeleton() {
  return (
    <div className="space-y-5 p-4">
      <Skeleton className="h-9 w-36" />
      <div className="grid grid-cols-2 gap-4">
        {[0, 1, 2, 3].map((item) => (
          <Skeleton key={item} className="h-10 w-full" />
        ))}
      </div>
    </div>
  );
}

function TableSkeleton() {
  return (
    <div className="space-y-3 p-4">
      {[0, 1, 2, 3].map((item) => (
        <Skeleton key={item} className="h-10 w-full" />
      ))}
    </div>
  );
}

function LoadFailure({ onRetry, isRetrying }: { onRetry: () => void; isRetrying: boolean }) {
  return (
    <Panel title="Usage unavailable" className="min-h-[18rem]">
      <div className="flex h-full min-h-64 items-center justify-center p-6" role="alert">
        <div className="max-w-sm text-center">
          <TriangleAlert className="mx-auto size-5 text-destructive" />
          <h2 className="mt-3 text-sm font-medium">Billing report could not be loaded</h2>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
            The usage service did not return this window. Retry without changing the selected range.
          </p>
          <Button
            className="mt-4"
            variant="outline"
            size="sm"
            onClick={onRetry}
            disabled={isRetrying}
          >
            <RefreshCw className={cn(isRetrying && "animate-spin")} />
            {isRetrying ? "Retrying" : "Retry"}
          </Button>
        </div>
      </div>
    </Panel>
  );
}

function formatUsageWindow(window: UsageWindow): string {
  const formatter = new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  });
  return `${formatter.format(new Date(window.start))} - ${formatter.format(new Date(window.end))}`;
}

function downloadBlob(filename: string, blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
