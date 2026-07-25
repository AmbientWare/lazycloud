import {
  useInfiniteQuery,
  useQuery,
  type UseQueryResult,
} from "@tanstack/react-query";

import { ApiErrorNotice } from "@/components/shared/ApiErrorNotice";
import { CliHint } from "@/components/shared/CliHint";
import {
  ChartSkeleton,
  ContainerMetricsCharts,
} from "@/components/shared/ContainerMetricsCharts";
import { PanelErrorBoundary } from "@/components/shared/ErrorBoundary";
import { ShellButton } from "@/components/shared/ShellDialog";
import { StatusChip } from "@/components/shared/StatusChip";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import type {
  ContainerDetail,
  ContainerMetricsTimeseries,
  Deployment,
} from "@/lib/api/schemas";
import { relativeTime } from "@/lib/format";
import {
  containerMetricsTimeseriesQueryOptions,
  containerQueryOptions,
} from "@/lib/queries/containers";
import {
  deploymentsInfiniteQueryOptions,
  selectDeploymentList,
} from "@/lib/queries/deployments";
import { cn } from "@/lib/utils";

import { currentDeployment, findWorkloadGroup } from "./grouping";
import { podInstancePlacement, podInstanceUptime } from "./pod-instance-format";

export function PodInstanceDrawer({
  workspaceId,
  appId,
  workloadName,
  containerId,
  onClose,
}: {
  workspaceId: string;
  appId: string;
  workloadName: string;
  containerId: string;
  onClose: () => void;
}) {
  const container = useQuery(containerQueryOptions(workspaceId, containerId));
  const deployments = useInfiniteQuery(
    deploymentsInfiniteQueryOptions(workspaceId, { appId, name: workloadName }),
  );
  const deploymentList = selectDeploymentList(deployments.data, deployments.hasNextPage);
  const group = findWorkloadGroup(deploymentList.items, appId, workloadName);
  const deployment = group?.kind === "pod" ? currentDeployment(group) : undefined;
  const member = Boolean(
    container.data && deployment?.stub_id && container.data.stub_id === deployment.stub_id,
  );
  const metrics = useQuery({
    ...containerMetricsTimeseriesQueryOptions(
      workspaceId,
      containerId,
      container.data?.status === "running",
    ),
    enabled: member,
  });

  const pending = container.isPending || deployments.isPending;
  const error = container.error ?? deployments.error;
  const membershipError =
    !pending && !error && (!container.data || !deployment || !member)
      ? new Error("This instance is not part of the current Pod deployment")
      : null;

  return (
    <Sheet open onOpenChange={(open) => (open ? undefined : onClose())}>
      <SheetContent
        aria-describedby={undefined}
        className="gap-0 bg-background max-sm:left-0 max-sm:right-0 max-sm:max-w-none max-sm:border-l-0 sm:max-w-3xl xl:max-w-4xl"
      >
        {pending ? (
          <PodInstanceDrawerSkeleton />
        ) : error || membershipError || !container.data || !deployment ? (
          <div className="flex min-h-0 flex-1 flex-col">
            <SheetTitle className="sr-only">Pod instance</SheetTitle>
            <div className="flex min-h-0 flex-1 items-center justify-center p-4">
              <ApiErrorNotice
                error={error ?? membershipError ?? new Error("Pod instance could not be loaded")}
                title="Pod instance could not be loaded"
                onRetry={() => {
                  void Promise.all([container.refetch(), deployments.refetch()]);
                }}
                retrying={container.isFetching || deployments.isFetching}
                className="panel w-full max-w-lg rounded-md"
              />
            </div>
          </div>
        ) : (
          <PodInstanceDrawerBody
            record={container.data}
            deployment={deployment}
            workloadName={workloadName}
            metrics={metrics}
          />
        )}
      </SheetContent>
    </Sheet>
  );
}

function PodInstanceDrawerBody({
  record,
  deployment,
  workloadName,
  metrics,
}: {
  record: ContainerDetail;
  deployment: Deployment;
  workloadName: string;
  metrics: UseQueryResult<ContainerMetricsTimeseries, Error>;
}) {
  const running = record.status === "running";
  const resources = deployment.spec.resources;
  const latestTimestamp = metrics.data?.points.at(-1)?.timestamp;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="shrink-0 border-b border-border bg-card px-4 py-3 pr-12">
        <div className="flex min-w-0 flex-wrap items-center gap-2.5">
          <SheetTitle>Pod instance</SheetTitle>
          <StatusChip status={record.status} live={running} />
          <div className="ml-auto">
            {record.actions.can_shell ? (
              <ShellButton containerId={record.id} running={running} />
            ) : null}
          </div>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          <span className="mono text-foreground">{workloadName}</span>
          <span aria-hidden="true"> · </span>
          <span>v{deployment.version}</span>
          {record.started_at ? (
            <>
              <span aria-hidden="true"> · </span>
              <time dateTime={record.started_at} title={exactTime(record.started_at)}>
                Started {relativeTime(record.started_at)}
              </time>
            </>
          ) : null}
        </p>
      </header>

      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3">
        <section className="panel shrink-0 overflow-hidden rounded-md" aria-label="Instance summary">
          <dl className="grid grid-cols-2 gap-x-5 gap-y-4 p-4 sm:grid-cols-4">
            <DrawerFact
              label="Placement"
              value={podInstancePlacement(record)}
              title={
                record.runtime_machine_id ||
                record.machine_id ||
                record.runtime_worker_id ||
                record.worker_id ||
                undefined
              }
            />
            <DrawerFact label="Uptime" value={podInstanceUptime(record)} mono />
            <DrawerFact
              label="CPU allocation"
              value={resources.cpu == null ? "Default" : `${resources.cpu} vCPU`}
              mono
            />
            <DrawerFact label="Memory allocation" value={resources.memory ?? "Default"} mono />
            {resources.gpu ? (
              <DrawerFact
                label="GPU allocation"
                value={`${resources.gpu}${resources.gpu_count > 1 ? ` x${resources.gpu_count}` : ""}`}
                mono
              />
            ) : null}
          </dl>
          {record.actions.can_shell && running ? (
            <section
              aria-label="Terminal access"
              className="flex flex-col gap-2 border-t border-border/80 bg-muted/10 p-3 sm:flex-row sm:items-center sm:px-4"
            >
              <div className="min-w-0">
                <h3 className="text-xs font-medium text-foreground">Terminal access</h3>
                <p className="mt-0.5 text-[11px] text-muted-foreground">
                  Uses your authenticated CLI profile
                </p>
              </div>
              <CliHint
                command={`lazycloud shell --container-id ${record.id}`}
                className="min-w-0 flex-1 border-0 bg-muted/40 py-1.5"
              />
            </section>
          ) : null}
        </section>

        <section className="panel min-h-[24rem] shrink-0 overflow-hidden rounded-md" aria-label="Instance compute">
          <div className="flex min-h-11 items-center justify-between gap-3 border-b border-border/80 px-4 py-2.5">
            <div>
              <h3 className="text-sm font-medium">Compute</h3>
              <p className="mt-0.5 text-[11px] text-muted-foreground">
                Live utilization for this instance
              </p>
            </div>
            {latestTimestamp ? (
              <time
                dateTime={latestTimestamp}
                title={exactTime(latestTimestamp)}
                className="text-[11px] text-muted-foreground"
              >
                Updated {relativeTime(latestTimestamp)}
              </time>
            ) : null}
          </div>
          <div className="p-4">
            {metrics.isPending ? (
              <div className="grid gap-x-6 gap-y-5 lg:grid-cols-2">
                <ChartSkeleton />
                <ChartSkeleton />
              </div>
            ) : metrics.isError ? (
              <div className="flex h-44 items-center justify-center text-sm text-destructive" role="alert">
                {metrics.error.message}
              </div>
            ) : (
              <PanelErrorBoundary title="Instance metrics could not be displayed">
                <ContainerMetricsCharts points={metrics.data?.points} />
              </PanelErrorBoundary>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

function PodInstanceDrawerSkeleton() {
  return (
    <div className="flex min-h-0 flex-1 flex-col" aria-hidden="true">
      <SheetTitle className="sr-only">Pod instance</SheetTitle>
      <div className="flex min-h-14 items-center gap-2.5 border-b border-border bg-card px-4 py-3 pr-12">
        <Skeleton className="h-5 w-32" />
        <Skeleton className="h-5 w-16" />
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-hidden p-3">
        <Skeleton className="h-56 w-full shrink-0" />
        <Skeleton className="min-h-80 w-full flex-1" />
      </div>
    </div>
  );
}

function DrawerFact({
  label,
  value,
  mono = false,
  title,
}: {
  label: string;
  value: string;
  mono?: boolean;
  title?: string;
}) {
  return (
    <div className="min-w-0">
      <dt className="micro-label mb-1">{label}</dt>
      <dd className={cn("truncate text-sm", mono && "mono tabular-nums")} title={title ?? value}>
        {value}
      </dd>
    </div>
  );
}

function exactTime(value: string): string {
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? value : timestamp.toLocaleString();
}
