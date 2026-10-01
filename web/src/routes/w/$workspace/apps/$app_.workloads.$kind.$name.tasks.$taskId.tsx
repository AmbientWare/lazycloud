import { createFileRoute, useNavigate } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { TaskDrawer } from "@/components/shared/TaskDrawer";
import { useWorkspace } from "@/lib/workspace-context";

export const Route = createFileRoute(
  "/w/$workspace/apps/$app_/workloads/$kind/$name/tasks/$taskId",
)({
  component: WorkloadTaskDrawerRoute,
  errorComponent: RouteErrorFallback,
});

function WorkloadTaskDrawerRoute() {
  const { app, kind, name, taskId } = Route.useParams();
  const { workspace } = useWorkspace();
  const navigate = useNavigate();

  return (
    <TaskDrawer
      taskId={taskId}
      taskLink={(nextTaskId) => ({
        to: "/w/$workspace/apps/$app/workloads/$kind/$name/tasks/$taskId",
        params: {
          workspace: workspace.name,
          app,
          kind,
          name,
          taskId: nextTaskId,
        },
      })}
      onClose={() => {
        void navigate({
          to: "/w/$workspace/apps/$app/workloads/$kind/$name",
          params: { workspace: workspace.name, app, kind, name },
        });
      }}
    />
  );
}
