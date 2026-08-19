import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { ChartSkeleton, ContainerMetricsCharts } from "@/components/shared/ContainerMetricsCharts";
import { CopyId } from "@/components/shared/CopyId";
import { Fact } from "@/components/shared/Fact";
import { FactGrid } from "@/components/shared/Fact/FactGrid";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { StatusChip } from "@/components/shared/StatusChip";
import { StopCause } from "@/components/shared/StopCause";
import { Skeleton } from "@/components/ui/skeleton";
import type { Container, ContainerMetricsPoint, Task } from "@/lib/api/schemas";
import { durationBetween, exactTime, formatBytes, relativeTime } from "@/lib/format";
import { containerMetricsTimeseriesQueryOptions } from "@/lib/queries/containers";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

export function ContainerTab({
  record,
  workspaceId,
  live,
}: {
  record: Task;
  workspaceId: string;
  live: boolean;
}) {
  if (!record.container_id || !record.container) {
    const pending = ["pending", "retry", "running"].includes(record.status);
    return (
      <PanelEmpty
        message={
          pending ? "Waiting for a container" : "Container details are unavailable for this task"
        }
        className="h-full min-h-48 p-6"
      />
    );
  }

  return (
    <ContainerDetails
      container={record.container}
      containerId={record.container_id}
      workspaceId={workspaceId}
      live={live}
    />
  );
}

function ContainerDetails({
  container,
  containerId,
  workspaceId,
  live,
}: {
  container: Container;
  containerId: string;
  workspaceId: string;
  live: boolean;
}) {
  const queryClient = useQueryClient();
  const metrics = useQuery(containerMetricsTimeseriesQueryOptions(workspaceId, containerId, live));

  // Capture the last recorded sample after a live task terminates.
  const wasLive = useRef(live);
  useEffect(() => {
    if (wasLive.current && !live) {
      void queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.containers.metrics(workspaceId, containerId),
      });
    }
    wasLive.current = live;
  }, [live, queryClient, workspaceId, containerId]);

  const running = container.status === "running";
  const command = container.command.join(" ");
  const ports = [...new Set(Object.values(container.ports))].sort((a, b) => a - b);
  const facts = [
    { label: "Container ID", value: <CopyId value={containerId} className="-ml-1.5" /> },
    { label: "Image", value: container.image || "None", mono: true },
    {
      label: "Worker",
      value: container.runtime_worker_id || container.worker_id || "None",
      mono: true,
    },
    {
      label: "Machine",
      value: container.runtime_machine_id || container.machine_id || "None",
      mono: true,
    },
    { label: "Command", value: command || "None", mono: true },
    { label: "Working directory", value: container.cwd || "Default", mono: true },
    { label: "Ports", value: ports.length ? ports.join(", ") : "None", mono: true },
    {
      label: "Exit code",
      value:
        container.exit_code === null || container.exit_code === undefined
          ? "None"
          : String(container.exit_code),
      mono: true,
    },
    {
      label: "Created",
      value: relativeTime(container.created_at),
      title: exactTime(container.created_at),
    },
    {
      label: "Started",
      value: container.started_at ? relativeTime(container.started_at) : "Not started",
      title: container.started_at ? exactTime(container.started_at) : undefined,
    },
    {
      label: "Uptime",
      value: durationBetween(container.started_at, container.finished_at) ?? "Not started",
    },
  ];
  const latest = latestSample(metrics.data?.points);

  return (
    <div className="space-y-5 p-4">
      <section aria-labelledby="container-identity-heading">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <h3 id="container-identity-heading" className="mono min-w-0 truncate text-sm font-medium">
            {container.name || containerId}
          </h3>
          <StatusChip status={container.status} live={running} />
        </div>
        <FactGrid columns={3} className="mt-4">
          {facts.map((fact) => (
            <Fact
              key={fact.label}
              label={fact.label}
              value={fact.value}
              mono={fact.mono}
              title={fact.title}
            />
          ))}
        </FactGrid>
        <StopCause
          terminationReason={container.termination_reason}
          status={container.status}
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
            <ContainerCapacity sample={latest} />
            <ContainerMetricsCharts points={metrics.data.points} className="mt-5 sm:grid-cols-2" />
          </>
        )}
      </section>
    </div>
  );
}

function ContainerCapacity({ sample }: { sample: ContainerMetricsPoint | undefined }) {
  const capacity = [
    {
      label: "CPU allocation",
      value: sample ? formatCpu(sample.cpu_total_millicores) : "Not reported",
    },
    {
      label: "Memory allocation",
      value: sample?.memory_total_bytes ? formatBytes(sample.memory_total_bytes) : "Not reported",
    },
    {
      label: "GPU",
      value: sample?.gpu_memory_total_bytes
        ? `${sample.gpu_type || "GPU"} · ${formatBytes(sample.gpu_memory_total_bytes)}`
        : "None",
    },
  ];
  return (
    <FactGrid columns={3} className="mt-3 gap-y-3">
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
  points: ContainerMetricsPoint[] | undefined,
): ContainerMetricsPoint | undefined {
  return points?.reduce<ContainerMetricsPoint | undefined>((latest, point) => {
    if (!latest || point.timestamp > latest.timestamp) return point;
    return latest;
  }, undefined);
}

function formatCpu(millicores: number): string {
  if (millicores <= 0) return "Not reported";
  const vcpus = Number((millicores / 1_000).toFixed(2));
  return `${vcpus} vCPU`;
}
