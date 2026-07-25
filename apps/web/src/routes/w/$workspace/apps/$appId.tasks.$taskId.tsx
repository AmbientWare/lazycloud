import { createFileRoute, useNavigate } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { TaskDrawer } from "@/components/shared/TaskDrawer";
import { useWorkspace } from "@/lib/workspace-context";

export const Route = createFileRoute("/w/$workspace/apps/$appId/tasks/$taskId")({
  component: AppTaskDrawerRoute,
  errorComponent: RouteErrorFallback,
});

function AppTaskDrawerRoute() {
  const { appId, taskId } = Route.useParams();
  const { workspace } = useWorkspace();
  const navigate = useNavigate();

  return (
    <TaskDrawer
      taskId={taskId}
      taskLink={(nextTaskId) => ({
        to: "/w/$workspace/apps/$appId/tasks/$taskId",
        params: { workspace: workspace.name, appId, taskId: nextTaskId },
      })}
      onClose={() => {
        void navigate({
          to: "/w/$workspace/apps/$appId",
          params: { workspace: workspace.name, appId },
        });
      }}
    />
  );
}
