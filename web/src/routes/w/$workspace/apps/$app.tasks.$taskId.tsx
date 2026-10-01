import { createFileRoute, useNavigate } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { TaskDrawer } from "@/components/shared/TaskDrawer";
import { useWorkspace } from "@/lib/workspace-context";

export const Route = createFileRoute("/w/$workspace/apps/$app/tasks/$taskId")({
  component: AppTaskDrawerRoute,
  errorComponent: RouteErrorFallback,
});

function AppTaskDrawerRoute() {
  const { app, taskId } = Route.useParams();
  const { workspace } = useWorkspace();
  const navigate = useNavigate();

  return (
    <TaskDrawer
      taskId={taskId}
      taskLink={(nextTaskId) => ({
        to: "/w/$workspace/apps/$app/tasks/$taskId",
        params: { workspace: workspace.name, app, taskId: nextTaskId },
      })}
      onClose={() => {
        void navigate({
          to: "/w/$workspace/apps/$app",
          params: { workspace: workspace.name, app },
        });
      }}
    />
  );
}
