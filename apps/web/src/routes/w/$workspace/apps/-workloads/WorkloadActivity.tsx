import { useQuery } from "@tanstack/react-query";

import { Panel } from "@/components/shared/Panel";
import { TaskTable } from "@/components/shared/TaskTable";
import { tasksQueryOptions } from "@/lib/queries/tasks";

export function WorkloadActivity({
  workspaceId,
  workspaceName,
  appId,
  workloadName,
  stubIds,
}: {
  workspaceId: string;
  workspaceName: string;
  appId: string;
  workloadName: string;
  stubIds: string[];
}) {
  return (
    <Panel
      title="Activity"
      className="min-h-[22rem] shrink-0 lg:h-full lg:min-h-0"
      contentClassName="overflow-hidden p-0"
    >
      <WorkloadRuns
        workspaceId={workspaceId}
        workspaceName={workspaceName}
        appId={appId}
        workloadName={workloadName}
        stubIds={stubIds}
      />
    </Panel>
  );
}

function WorkloadRuns({
  workspaceId,
  workspaceName,
  appId,
  workloadName,
  stubIds,
}: {
  workspaceId: string;
  workspaceName: string;
  appId: string;
  workloadName: string;
  stubIds: string[];
}) {
  const tasks = useQuery(tasksQueryOptions(workspaceId, { limit: 50, appId, stubIds }));
  if (tasks.isError) {
    return <div className="p-4 text-sm text-destructive">{tasks.error.message}</div>;
  }
  return (
    <TaskTable
      tasks={tasks.isPending ? undefined : tasks.data?.data}
      showApp={false}
      taskLink={(taskId) => ({
        to: "/w/$workspace/apps/$appId/workloads/$name/tasks/$taskId",
        params: { workspace: workspaceName, appId, name: workloadName, taskId },
      })}
      emptyMessage="No tasks recorded yet"
      className="h-full"
    />
  );
}
