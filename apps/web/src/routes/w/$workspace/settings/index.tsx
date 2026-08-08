import { createFileRoute } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { LinearTab, LinearTabsList } from "@/components/shared/LinearSelect";
import { WorkspaceDeletionPanel } from "@/components/shared/WorkspaceDeletion/Panel";
import { useWorkspaceDeletion } from "@/components/shared/WorkspaceDeletion/context";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { useWorkspace } from "@/lib/workspace-context";

import { AccountSettings } from "./-components/AccountSettings";
import { ComputeSettings } from "./-components/ComputeSettings";
import { DomainSettings } from "./-components/DomainSettings";
import { WorkspaceIdentity } from "./-components/WorkspaceIdentity";
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
          <div className="space-y-4 pb-1">
            <AccountSettings />
            <Accordion type="single" collapsible defaultValue="workspace">
              <AccordionItem value="workspace" className="panel rounded-md border-b-0 px-4">
                <AccordionTrigger className="hover:no-underline">
                  <span className="flex min-w-0 flex-col gap-0.5 text-left">
                    <span className="truncate text-sm font-medium">Workspace</span>
                    <span className="mono truncate text-[11px] font-normal text-muted-foreground">
                      {workspace.name}
                    </span>
                  </span>
                </AccordionTrigger>
                <AccordionContent className="grid gap-4 lg:grid-cols-5">
                  <WorkspaceIdentity workspace={workspace} fullWidth={!deletion.canManage} />
                  <WorkspaceTokens workspaceId={workspace.id} />
                  <WorkspaceDeletionPanel workspace={workspace} />
                </AccordionContent>
              </AccordionItem>
            </Accordion>
          </div>
        </TabsContent>
        <TabsContent
          value="compute"
          className="mt-3 min-h-0 flex-1 overflow-visible lg:overflow-hidden"
        >
          <ComputeSettings workspaceId={workspace.id} />
        </TabsContent>
        <TabsContent
          value="domains"
          className="mt-3 min-h-0 flex-1 overflow-visible lg:overflow-hidden"
        >
          <DomainSettings workspaceId={workspace.id} />
        </TabsContent>
      </Tabs>
    </WorkspacePage>
  );
}
