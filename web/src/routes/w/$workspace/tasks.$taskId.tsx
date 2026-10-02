import { createFileRoute, useNavigate } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { TaskDrawer } from "@/components/shared/TaskDrawer";
import { useWorkspace } from "@/lib/workspace-context";

export const Route = createFileRoute("/w/$workspace/tasks/$taskId")({
  component: GlobalTaskDrawerRoute,
  errorComponent: RouteErrorFallback,
});

function GlobalTaskDrawerRoute() {
  const { taskId } = Route.useParams();
  const search = Route.useSearch();
  const { workspace } = useWorkspace();
  const navigate = useNavigate();

  return (
    <TaskDrawer
      taskId={taskId}
      taskLink={(nextTaskId) => ({
        to: "/w/$workspace/tasks/$taskId",
        params: { workspace: workspace.name, taskId: nextTaskId },
        search,
      })}
      onClose={() => {
        void navigate({
          to: "/w/$workspace/tasks",
          params: { workspace: workspace.name },
          search,
        });
      }}
    />
  );
}
