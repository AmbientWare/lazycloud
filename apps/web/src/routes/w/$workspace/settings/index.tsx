import { createFileRoute } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { LinearTab, LinearTabsList } from "@/components/shared/LinearSelect";
import { WorkspaceDeletionPanel } from "@/components/shared/WorkspaceDeletion/Panel";
import { useWorkspaceDeletion } from "@/components/shared/WorkspaceDeletion/context";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { useWorkspace } from "@/lib/workspace-context";

import { AccountSettings } from "./-components/AccountSettings";
import { ComputeSettings } from "./-components/ComputeSettings";
import { DomainSettings } from "./-components/DomainSettings";
import { WorkspaceIdentity } from "./-components/WorkspaceIdentity";
import { WorkspaceSection } from "./-components/WorkspaceSection";
import { WorkspaceTokens } from "./-components/WorkspaceTokens";

export const Route = createFileRoute("/w/$workspace/settings/")({
  validateSearch: (search: Record<string, unknown>) => ({
    view:
      search.view === "compute"
        ? ("compute" as const)
        : search.view === "domains"
          ? ("domains" as const)
          : ("general" as const),
  }),
  component: SettingsPage,
  errorComponent: RouteErrorFallback,
});

function SettingsPage() {
  const { workspace } = useWorkspace();
  const deletion = useWorkspaceDeletion();
  const search = Route.useSearch();
  const navigate = Route.useNavigate();

  return (
    <WorkspacePage title="Settings" contentClassName="min-h-0 overflow-y-auto lg:overflow-hidden">
      <Tabs
        value={search.view}
        onValueChange={(view) => {
          void navigate({
            search: {
              view: view === "compute" ? "compute" : view === "domains" ? "domains" : "general",
            },
            replace: true,
          });
        }}
        className="flex min-h-full flex-col overflow-visible lg:h-full lg:min-h-0 lg:overflow-hidden"
      >
        <LinearTabsList ariaLabel="Settings sections">
          <LinearTab value="general">General</LinearTab>
          <LinearTab value="compute">Compute</LinearTab>
          <LinearTab value="domains">Domains</LinearTab>
        </LinearTabsList>
        <TabsContent value="general" className="mt-3 min-h-0 flex-1 overflow-y-auto">
          {/* The account comes first and reads the same in every workspace, because it
              is not about one. What belongs to the workspace you happen to be in is
              collapsed below it, named so it is clear which workspace that is. */}
          <div className="space-y-5 pb-1">
            <AccountSettings />
            <WorkspaceSection
              workspaceName={workspace.name}
              contentClassName="grid gap-4 lg:grid-cols-5 lg:grid-rows-[11.5rem_minmax(0,1fr)]"
            >
              <WorkspaceIdentity workspace={workspace} fullWidth={!deletion.canManage} />
              <WorkspaceTokens workspaceId={workspace.id} />
              <WorkspaceDeletionPanel workspace={workspace} />
            </WorkspaceSection>
          </div>
        </TabsContent>
        <TabsContent value="compute" className="mt-3 min-h-0 flex-1 overflow-y-auto">
          <ComputeSettings workspaceId={workspace.id} workspaceName={workspace.name} />
        </TabsContent>
        <TabsContent value="domains" className="mt-3 min-h-0 flex-1 overflow-y-auto">
          <DomainSettings workspaceId={workspace.id} />
        </TabsContent>
      </Tabs>
    </WorkspacePage>
  );
}
