import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { ApiErrorNotice } from "@/components/shared/ApiErrorNotice";
import { CliHint } from "@/components/shared/CliHint";
import { Fact } from "@/components/shared/Fact";
import { FactGrid } from "@/components/shared/Fact/FactGrid";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { ChartSkeleton, ContainerMetricsCharts } from "@/components/shared/ContainerMetricsCharts";
import { PanelErrorBoundary } from "@/components/shared/ErrorBoundary";
import { PanelError } from "@/components/shared/PanelError";
import { ShellButton } from "@/components/shared/ShellDialog";
import { DrawerHeader, DrawerHeaderSkeleton } from "@/components/shared/DrawerHeader";
import { StatusChip } from "@/components/shared/StatusChip";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import type { ContainerDetail, ContainerMetricsTimeseries, Deployment } from "@/lib/api/schemas";
import { StopCause } from "@/components/shared/StopCause";
import { resourceAllocation } from "@/lib/format";
import {
  containerMetricsTimeseriesQueryOptions,
  containerQueryOptions,
} from "@/lib/queries/containers";
import { workloadQueryOptions } from "@/lib/queries/deployments";

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
  const workload = useQuery(workloadQueryOptions(workspaceId, appId, workloadName));
  const deployment = workload.data?.deployment;
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

  const pending = container.isPending || workload.isPending;
  const error = container.error ?? workload.error;
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
                  void Promise.all([container.refetch(), workload.refetch()]);
                }}
                retrying={container.isFetching || workload.isFetching}
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
      <DrawerHeader>
        <div className="flex min-w-0 flex-wrap items-center gap-2.5">
          <SheetTitle>Pod instance</SheetTitle>
          <StatusChip status={record.status} />
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
              <span>
                Started <LiveRelativeTime value={record.started_at} />
              </span>
            </>
          ) : null}
        </p>
      </DrawerHeader>

      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3">
        <section
          className="panel shrink-0 overflow-hidden rounded-md"
          aria-label="Instance summary"
        >
          <FactGrid columns={4} className="gap-x-5 p-4">
            <Fact
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
            <Fact label="Uptime" value={podInstanceUptime(record)} mono />
            <Fact label="CPU allocation" value={resourceAllocation(resources.cpu, "vCPU")} mono />
            <Fact label="Memory allocation" value={resourceAllocation(resources.memory)} mono />
            {resources.gpu.length > 0 ? (
              <Fact
                label="GPU allocation"
                value={`${resources.gpu.join(" → ")}${resources.gpu_count > 1 ? ` x${resources.gpu_count}` : ""}`}
                mono
              />
            ) : null}
          </FactGrid>
          <StopCause
            terminationReason={record.termination_reason}
            status={record.status}
            className="border-t border-border/80 px-4 py-3"
          />
          {record.actions.can_shell && running ? (
            <section
              aria-label="Terminal access"
              className="flex flex-col gap-2 border-t border-border/80 bg-muted/10 p-3 sm:flex-row sm:items-center sm:px-4"
            >
              <div className="min-w-0">
                <h3 className="text-xs font-medium text-foreground">Terminal access</h3>
              </div>
              <CliHint
                command={`lazycloud shell --container-id ${record.id}`}
                className="min-w-0 flex-1 border-0 bg-muted/40 py-1.5"
              />
            </section>
          ) : null}
        </section>

        <section
          className="panel min-h-[24rem] shrink-0 overflow-hidden rounded-md"
          aria-label="Instance compute"
        >
          <div className="flex min-h-11 items-center justify-between gap-3 border-b border-border/80 px-4 py-2.5">
            <div>
              <h3 className="text-sm font-medium">Compute</h3>
            </div>
            {latestTimestamp ? (
              <span className="text-[11px] text-muted-foreground">
                Updated <LiveRelativeTime value={latestTimestamp} />
              </span>
            ) : null}
          </div>
          <div className="p-4">
            {metrics.isPending ? (
              <div className="grid gap-x-6 gap-y-5 lg:grid-cols-2">
                <ChartSkeleton />
                <ChartSkeleton />
              </div>
            ) : metrics.isError ? (
              <PanelError message={metrics.error.message} layout="centered" />
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
      <DrawerHeaderSkeleton>
        <Skeleton className="h-5 w-32" />
        <Skeleton className="h-5 w-16" />
      </DrawerHeaderSkeleton>
      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-hidden p-3">
        <Skeleton className="h-56 w-full shrink-0" />
        <Skeleton className="min-h-80 w-full flex-1" />
      </div>
    </div>
  );
}
