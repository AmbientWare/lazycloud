import { useQuery } from "@tanstack/react-query";

import { PanelErrorBoundary } from "@/components/shared/ErrorBoundary";
import { Panel } from "@/components/shared/Panel";
import { PanelError } from "@/components/shared/PanelError";
import { TaskTable } from "@/components/shared/TaskTable";
import type { Schemas } from "@/lib/api/client";
import { performanceQueryOptions } from "@/lib/queries/deployments";
import { requestsQueryOptions, servesRequests, tasksQueryOptions } from "@/lib/queries/tasks";

import { LatencyPanel } from "./LatencyPanel";

/**
 * A function's, endpoint's or ASGI app's latency over the last day above its
 * newest tasks or requests. The change stream refreshes both.
 */
export function WorkloadActivity({
  workspace,
  workload,
}: {
  workspace: string;
  workload: Schemas["Workload"];
}) {
  const latency = useQuery(performanceQueryOptions(workspace, workload));

  return (
    <Panel
      title="Activity"
      contentClassName="flex flex-col overflow-hidden p-0"
      className="min-h-[24rem] shrink-0 xl:min-h-0"
    >
      <div className="h-52 shrink-0 border-b border-border/80 p-3">
        <PanelErrorBoundary title="Performance could not be displayed">
          <LatencyPanel
            buckets={latency.data?.buckets}
            pending={latency.isPending}
            error={latency.error}
            kind={workload.kind}
          />
        </PanelErrorBoundary>
      </div>
      <WorkloadRuns workspace={workspace} workload={workload} />
    </Panel>
  );
}

function WorkloadRuns({
  workspace,
  workload,
}: {
  workspace: string;
  workload: Schemas["Workload"];
}) {
  const { app, kind, name } = workload;
  const requests = servesRequests(kind);
  const tasks = useQuery({
    ...tasksQueryOptions(workspace, { app, function: name }),
    enabled: !requests,
  });
  const served = useQuery({ ...requestsQueryOptions(workspace, app, name), enabled: requests });
  const rows = requests ? served : tasks;
  if (rows.isError) {
    return <PanelError message={rows.error.message} />;
  }
  return (
    <TaskTable
      tasks={rows.isPending ? undefined : rows.data}
      showApp={false}
      showWorkload={false}
      taskLink={(taskId) => ({
        to: "/w/$workspace/apps/$app/workloads/$kind/$name/tasks/$taskId",
        params: { workspace, app, kind, name, taskId },
      })}
      emptyMessage="No tasks yet"
      compact
      className="min-h-0 flex-1"
    />
  );
}
