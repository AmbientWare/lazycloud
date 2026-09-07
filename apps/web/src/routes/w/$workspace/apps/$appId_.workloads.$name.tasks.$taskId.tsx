import { createFileRoute, useNavigate } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { TaskDrawer } from "@/components/shared/TaskDrawer";
import { useWorkspace } from "@/lib/workspace-context";

export const Route = createFileRoute("/w/$workspace/apps/$appId_/workloads/$name/tasks/$taskId")({
  component: WorkloadTaskDrawerRoute,
  errorComponent: RouteErrorFallback,
});

function WorkloadTaskDrawerRoute() {
  const { appId, name, taskId } = Route.useParams();
  const { kind } = Route.useSearch();
  const { workspace } = useWorkspace();
  const navigate = useNavigate();

  return (
    <TaskDrawer
      taskId={taskId}
      taskLink={(nextTaskId) => ({
        to: "/w/$workspace/apps/$appId/workloads/$name/tasks/$taskId",
        search: { kind },
        params: {
          workspace: workspace.name,
          appId,
          name,
          taskId: nextTaskId,
        },
      })}
      onClose={() => {
        void navigate({
          to: "/w/$workspace/apps/$appId/workloads/$name",
          params: { workspace: workspace.name, appId, name },
          search: { kind },
        });
      }}
    />
  );
}
