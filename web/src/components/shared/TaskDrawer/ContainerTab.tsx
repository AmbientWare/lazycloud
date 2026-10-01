import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { ChartSkeleton, ContainerMetricsCharts } from "@/components/shared/ContainerMetricsCharts";
import { CopyId } from "@/components/shared/CopyId";
import { Fact } from "@/components/shared/Fact";
import { FactGrid } from "@/components/shared/Fact/FactGrid";
import { LiveDuration, LiveRelativeTime } from "@/components/shared/LiveTime";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { StatusChip } from "@/components/shared/StatusChip";
import { StopCause } from "@/components/shared/StopCause";
import { Skeleton } from "@/components/ui/skeleton";
import { cpuAllocation, memoryAllocation } from "@/lib/format";
import {
  containerMetricsTimeseriesQueryOptions,
  containerQueryOptions,
  type Container,
} from "@/lib/queries/containers";
import type { Task } from "@/lib/queries/tasks";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

export function ContainerTab({
  task,
  workspace,
  live,
}: {
  task: Task;
  workspace: string;
  live: boolean;
}) {
  const container = useQuery({
    ...containerQueryOptions(workspace, task.container_id ?? ""),
    enabled: Boolean(task.container_id),
  });

  if (!task.container_id) {
    return (
      <PanelEmpty
        message={
          live ? "Waiting for a container" : "Container details are unavailable for this task"
        }
        className="h-full min-h-48 p-6"
      />
    );
  }
  if (container.isPending) {
    return (
      <div className="space-y-3 p-4" aria-hidden="true">
        <Skeleton className="h-5 w-56" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }
  if (container.isError) return <PanelError message={container.error.message} />;
  return <ContainerDetails container={container.data} workspace={workspace} live={live} />;
}

function ContainerDetails({
  container,
  workspace,
  live,
}: {
  container: Container;
  workspace: string;
  live: boolean;
}) {
  const queryClient = useQueryClient();
  const metrics = useQuery(containerMetricsTimeseriesQueryOptions(workspace, container.id, live));

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
    {
      label: "Workload",
      value: `${container.app}.${container.function}${container.version ? ` v${container.version}` : ""}`,
      mono: true,
    },
    { label: "CPU", value: cpuAllocation(container.cpu_millis), mono: true },
    { label: "Memory", value: memoryAllocation(container.memory_mib), mono: true },
    { label: "Slots", value: `${container.running_tasks}/${container.slots} busy`, mono: true },
    { label: "Created", value: <LiveRelativeTime value={container.created_at} /> },
    {
      label: "Ready",
      value: container.ready_at ? <LiveRelativeTime value={container.ready_at} /> : "Not ready",
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
            {container.id}
          </h3>
          <StatusChip status={container.state} live={container.state === "ready"} />
        </div>
        <FactGrid columns={3} className="mt-4">
          {facts.map((fact) => (
            <Fact key={fact.label} label={fact.label} value={fact.value} mono={fact.mono} />
          ))}
        </FactGrid>
        <StopCause reason={container.stop_reason} state={container.state} className="mt-4" />
        {container.exit_message ? (
          <pre className="mono mt-2 whitespace-pre-wrap break-all text-xs text-muted-foreground">
            {container.exit_message}
          </pre>
        ) : null}
      </section>

      <section className="border-t border-border pt-4" aria-labelledby="container-compute-heading">
        <h3 id="container-compute-heading" className="micro-label">
          Compute
        </h3>
        {metrics.isPending ? (
          <div className="mt-5 grid grid-cols-1 gap-x-6 gap-y-5 sm:grid-cols-2">
            <ChartSkeleton />
            <ChartSkeleton />
          </div>
        ) : metrics.isError ? (
          <PanelError message={metrics.error.message} />
        ) : (
          <ContainerMetricsCharts points={metrics.data.points} className="mt-5 sm:grid-cols-2" />
        )}
      </section>
    </div>
  );
}
