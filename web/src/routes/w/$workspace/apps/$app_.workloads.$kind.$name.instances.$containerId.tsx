import { createFileRoute, useNavigate } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { useWorkspace } from "@/lib/workspace-context";

import { PodInstanceDrawer } from "./-workloads/PodInstanceDrawer";

export const Route = createFileRoute(
  "/w/$workspace/apps/$app_/workloads/$kind/$name/instances/$containerId",
)({
  component: PodInstanceDrawerRoute,
  errorComponent: RouteErrorFallback,
});

function PodInstanceDrawerRoute() {
  const { app, kind, name, containerId } = Route.useParams();
  const { workspace } = useWorkspace();
  const navigate = useNavigate();

  return (
    <PodInstanceDrawer
      workspaceId={workspace.id}
      app={app}
      workloadName={name}
      workloadKind={kind}
      containerId={containerId}
      onClose={() => {
        void navigate({
          to: "/w/$workspace/apps/$app/workloads/$kind/$name",
          params: { workspace: workspace.name, app, kind, name },
        });
      }}
    />
  );
}
