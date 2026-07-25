import { createFileRoute, Navigate } from "@tanstack/react-router";

import { useSession } from "@/components/shared/AuthGate/session";
import { useWorkspaceSelection } from "@/lib/workspace-selection";

export const Route = createFileRoute("/dashboard")({
  component: DashboardEntry,
  head: () => ({
    meta: [{ title: "Dashboard — LazyCloud" }],
  }),
});

function DashboardEntry() {
  const { workspaces } = useSession();
  const lastWorkspaceName = useWorkspaceSelection((state) => state.lastWorkspaceName);
  const remembered = workspaces.find((item) => item.name === lastWorkspaceName);
  const target = remembered ?? workspaces[0];
  return <Navigate to="/w/$workspace" params={{ workspace: target.name }} replace />;
}
