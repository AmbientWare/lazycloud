import { createFileRoute, useNavigate } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { isWorkloadKind } from "@/lib/queries/deployments";
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
  // The workload page shows an unknown kind as not found.
  if (!isWorkloadKind(kind)) return null;

  return (
    <PodInstanceDrawer
      workspace={workspace.name}
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
