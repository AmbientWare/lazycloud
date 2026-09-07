import { createFileRoute, useNavigate } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { useWorkspace } from "@/lib/workspace-context";

import { PodInstanceDrawer } from "./-workloads/PodInstanceDrawer";

export const Route = createFileRoute(
  "/w/$workspace/apps/$appId_/workloads/$name/instances/$containerId",
)({
  beforeLoad: ({ search }) => {
    if (search.kind !== "pod") throw new Error("Instances are available for pod workloads.");
  },
  component: PodInstanceDrawerRoute,
  errorComponent: RouteErrorFallback,
});

function PodInstanceDrawerRoute() {
  const { appId, name, containerId } = Route.useParams();
  const { kind } = Route.useSearch();
  const { workspace } = useWorkspace();
  const navigate = useNavigate();

  return (
    <PodInstanceDrawer
      workspaceId={workspace.id}
      appId={appId}
      workloadName={name}
      containerId={containerId}
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
