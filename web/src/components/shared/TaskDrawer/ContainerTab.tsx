import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { ChartSkeleton, ContainerMetricsCharts } from "@/components/shared/ContainerMetricsCharts";
import { CopyId } from "@/components/shared/CopyId";
import { Fact } from "@/components/shared/Fact";
import { FactGrid } from "@/components/shared/Fact/FactGrid";
import { LiveDuration, LiveRelativeTime } from "@/components/shared/LiveTime";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { RowsSkeleton } from "@/components/shared/RowsSkeleton";
import { StatusChip } from "@/components/shared/StatusChip";
import { StopCause } from "@/components/shared/StopCause";
import { Skeleton } from "@/components/ui/skeleton";
import type { Schemas } from "@/lib/api/client";
import { formatBytes, shortId } from "@/lib/format";
import {
  containerLifecycleQueryOptions,
  containerMetricsQueryOptions,
  containerQueryOptions,
} from "@/lib/queries/containers";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

export function ContainerTab({
  workspace,
  containerId,
  waiting,
  live,
}: {
  workspace: string;
  /** The container of the latest attempt, or the one that served the request. */
  containerId: string | undefined;
  /** Whether the task may still be given a container. */
  waiting: boolean;
  live: boolean;
}) {
  const container = useQuery({
    ...containerQueryOptions(workspace, containerId ?? ""),
    enabled: Boolean(containerId),
  });
  if (!containerId) {
    return (
      <PanelEmpty
        message={
          waiting ? "Waiting for a container" : "Container details are unavailable for this task"
        }
        className="h-full min-h-48 p-6"
      />
    );
  }
  if (container.isPending) return <RowsSkeleton rows={4} height="h-10" />;
  if (container.isError) return <PanelError message={container.error.message} />;
  return <ContainerDetails container={container.data} workspace={workspace} live={live} />;
}

function ContainerDetails({
  container,
  workspace,
  live,
}: {
  container: Schemas["Container"];
  workspace: string;
  live: boolean;
}) {
  const queryClient = useQueryClient();
  const metrics = useQuery(containerMetricsQueryOptions(workspace, container.id, live));
  const lifecycle = useQuery(containerLifecycleQueryOptions(workspace, container.id));

  // Capture the last recorded sample after a live task terminates.
  const wasLive = useRef(live);
  useEffect(() => {
    if (wasLive.current && !live) {
      void queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.containers.metrics(workspace, container.id),
      });
    }
    wasLive.current = live;
  }, [live, queryClient, workspace, container.id]);

  const facts = [
    { label: "Container ID", value: <CopyId value={container.id} className="-ml-1.5" /> },
    { label: "Image", value: container.image || "None", mono: true },
    { label: "Machine", value: lifecycle.data?.host || "None", mono: true },
    { label: "Exit code", value: "None", mono: true },
    {
      label: "Created",
      value: <LiveRelativeTime value={container.created_at} />,
    },
    {
      label: "Started",
      value: container.ready_at ? <LiveRelativeTime value={container.ready_at} /> : "Not started",
    },
    {
      label: "Uptime",
      value: (
        <LiveDuration
          startedAt={container.ready_at}
          finishedAt={container.stopped_at}
          fallback="Not started"
        />
      ),
    },
  ];

  return (
    <div className="space-y-5 p-4">
      <section aria-labelledby="container-identity-heading">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <h3 id="container-identity-heading" className="mono min-w-0 truncate text-sm font-medium">
            {`${container.function}-${shortId(container.id)}`}
          </h3>
          <StatusChip status={container.state} live={container.state === "ready"} />
        </div>
        <FactGrid columns={3} className="mt-4">
          {facts.map((fact) => (
            <Fact key={fact.label} label={fact.label} value={fact.value} mono={fact.mono} />
          ))}
        </FactGrid>
        <StopCause
          reason={container.stop_reason}
          message={container.exit_message}
          exitCode={container.exit_code}
          className="mt-4"
        />
      </section>

      <section className="border-t border-border pt-4" aria-labelledby="container-compute-heading">
        <h3 id="container-compute-heading" className="micro-label">
          Compute
        </h3>
        {metrics.isPending ? (
          <>
            <CapacitySkeleton />
            <div className="mt-5 grid grid-cols-1 gap-x-6 gap-y-5 sm:grid-cols-2">
              <ChartSkeleton />
              <ChartSkeleton />
            </div>
          </>
        ) : metrics.isError ? (
          <PanelError message={metrics.error.message} />
        ) : (
          <>
            <ContainerCapacity metrics={metrics.data} />
            <ContainerMetricsCharts metrics={metrics.data} className="mt-5 sm:grid-cols-2" />
          </>
        )}
      </section>
    </div>
  );
}

function ContainerCapacity({ metrics }: { metrics: Schemas["ContainerMetrics"] }) {
  const sample = latestSample(metrics.points);
  const capacity = [
    {
      label: "CPU allocation",
      value: sample ? formatCpu(metrics.cpu_total_millicores) : "Not reported",
    },
    {
      label: "Memory allocation",
      value:
        sample && metrics.memory_total_bytes
          ? formatBytes(metrics.memory_total_bytes)
          : "Not reported",
    },
    {
      label: "GPU",
      value: sample?.gpu_memory_total_bytes
        ? `${sample.gpu_type || "GPU"} · ${formatBytes(sample.gpu_memory_total_bytes)}`
        : "None",
    },
  ];
  return (
    <FactGrid columns={3} className="content-transition mt-3 gap-y-3">
      {capacity.map((item) => (
        <Fact key={item.label} label={item.label} value={item.value} mono />
      ))}
    </FactGrid>
  );
}

function CapacitySkeleton() {
  return (
    <div className="mt-3 grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3" aria-hidden="true">
      {Array.from({ length: 3 }, (_, index) => (
        <div key={index} className="space-y-1.5">
          <Skeleton className="h-3 w-20" />
          <Skeleton className="h-4 w-24" />
        </div>
      ))}
    </div>
  );
}

function latestSample(
  points: Schemas["ContainerMetricPoint"][],
): Schemas["ContainerMetricPoint"] | undefined {
  return points.reduce<Schemas["ContainerMetricPoint"] | undefined>((latest, point) => {
    if (!latest || point.timestamp > latest.timestamp) return point;
    return latest;
  }, undefined);
}

function formatCpu(millicores: number): string {
  if (millicores <= 0) return "Not reported";
  const vcpus = Number((millicores / 1_000).toFixed(2));
  return `${vcpus} vCPU`;
}
