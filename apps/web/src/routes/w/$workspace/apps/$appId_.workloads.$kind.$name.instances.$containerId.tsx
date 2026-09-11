import { createFileRoute, useNavigate } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { useWorkspace } from "@/lib/workspace-context";

import { PodInstanceDrawer } from "./-workloads/PodInstanceDrawer";

export const Route = createFileRoute(
  "/w/$workspace/apps/$appId_/workloads/$kind/$name/instances/$containerId",
)({
  component: PodInstanceDrawerRoute,
  errorComponent: RouteErrorFallback,
});

function PodInstanceDrawerRoute() {
  const { appId, kind, name, containerId } = Route.useParams();
  const { workspace } = useWorkspace();
  const navigate = useNavigate();

  return (
    <PodInstanceDrawer
      workspaceId={workspace.id}
      appId={appId}
      workloadName={name}
      workloadKind={kind}
      containerId={containerId}
      onClose={() => {
        void navigate({
          to: "/w/$workspace/apps/$appId/workloads/$kind/$name",
          params: { workspace: workspace.name, appId, kind, name },
        });
      }}
    />
  );
}
