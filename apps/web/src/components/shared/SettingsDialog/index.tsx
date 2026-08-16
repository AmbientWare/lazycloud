import { useSession } from "@/components/shared/AuthGate/session";
import { LinearTab, LinearTabsList } from "@/components/shared/LinearSelect";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Tabs, TabsContent } from "@/components/ui/tabs";

import { AccessTokens } from "./AccessTokens";
import { AccountSettings } from "./AccountSettings";
import { BillingSettings } from "./BillingSettings";
import { ComputeSettings } from "./ComputeSettings";
import { DomainSettings } from "./DomainSettings";
import { settingsView, type SettingsView } from "./view";
import { WorkspaceAccordion } from "./WorkspaceAccordion";

/**
 * Settings, as a near-full-screen layer over whatever you were looking at.
 *
 * Nothing in here is addressed by the workspace in the URL — the connected clouds,
 * the machines, the domains, and the access tokens belong to the account, and the
 * workspaces appear as a list rather than as whichever one the sidebar selected. A
 * page under `/w/<workspace>/` would have claimed otherwise in its address.
 */
export function SettingsDialog({
  view,
  activeWorkspaceName,
  onViewChange,
  onClose,
}: {
  view: SettingsView;
  activeWorkspaceName: string;
  onViewChange: (view: SettingsView) => void;
  onClose: () => void;
}) {
  const { user, workspaces } = useSession();

  return (
    <Dialog open onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent
        className="flex h-[calc(100dvh-3rem)] w-[calc(100vw-3rem)] max-w-[76rem] flex-col gap-0 overflow-hidden p-0 sm:max-w-[76rem]"
      >
        <header className="shrink-0 border-b border-border px-5 py-3.5 pr-12">
          <DialogTitle className="text-base">Settings</DialogTitle>
          <DialogDescription className="mt-0.5 text-xs">
            Signed in as <span className="font-medium text-foreground/90">{user.display_name}</span>
          </DialogDescription>
        </header>

        <Tabs
          value={view}
          onValueChange={(next) => onViewChange(settingsView(next) ?? "general")}
          className="flex min-h-0 flex-1 flex-col overflow-hidden"
        >
          <div className="shrink-0 px-5 pt-3">
            <LinearTabsList ariaLabel="Settings sections">
              <LinearTab value="general">General</LinearTab>
              <LinearTab value="tokens">Tokens</LinearTab>
              <LinearTab value="compute">Compute</LinearTab>
              <LinearTab value="domains">Domains</LinearTab>
            </LinearTabsList>
          </div>

          <TabsContent value="general" className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
            <div className="space-y-5">
              <AccountSettings />
              <BillingSettings />
              <section>
                <h2 className="mb-1 text-sm font-medium">Workspaces</h2>
                <p className="mb-2 text-xs text-muted-foreground">
                  Every workspace you belong to. Compute and domains above apply to all of them.
                </p>
                <WorkspaceAccordion
                  workspaces={workspaces}
                  activeWorkspaceName={activeWorkspaceName}
                />
              </section>
            </div>
          </TabsContent>

          {/* The list keeps its own scroll region, and unmounting on a tab change is
              what drops a freshly issued secret without anything having to clear it. */}
          <TabsContent
            value="tokens"
            className="flex min-h-0 flex-1 flex-col overflow-hidden px-5 py-4"
          >
            <AccessTokens />
          </TabsContent>

          <TabsContent value="compute" className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
            <ComputeSettings />
          </TabsContent>

          <TabsContent value="domains" className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
            <DomainSettings />
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}
