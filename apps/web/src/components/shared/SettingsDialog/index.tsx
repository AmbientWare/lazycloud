import { useEffect, useState } from "react";

import { useSession } from "@/components/shared/AuthGate/session";
import { LinearTab, LinearTabsList } from "@/components/shared/LinearSelect";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Tabs, TabsContent } from "@/components/ui/tabs";

import { AccessTokens } from "./AccessTokens";
import { AccountSettings } from "./AccountSettings";
import { AdminSettings } from "./AdminSettings";
import { BillingSettings } from "./BillingSettings";
import { ComputeSettings } from "./ComputeSettings";
import { DomainSettings } from "./DomainSettings";
import { settingsView, type SettingsView } from "./view";

export function SettingsDialog({
  view,
  onViewChange,
  onClose,
}: {
  view: SettingsView;
  onViewChange: (view: SettingsView) => void;
  onClose: () => void;
}) {
  const { user } = useSession();
  const admin = user.role === "administrator";
  // A member can arrive at `?settings=admin` by typing it. The tab is not
  // rendered for them, so the view is shown as general and the address is
  // corrected to say so.
  const shownView = view === "admin" && !admin ? "general" : view;
  useEffect(() => {
    if (shownView !== view) onViewChange(shownView);
  }, [shownView, view, onViewChange]);
  const [planOpen, setPlanOpen] = useState(false);
  const openUpgrade = () => {
    setPlanOpen(true);
    onViewChange("general");
  };

  return (
    <Dialog open onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent className="flex h-[calc(100dvh-2rem)] w-[calc(100vw-2rem)] max-w-[76rem] flex-col gap-0 overflow-hidden p-0 sm:h-[min(48rem,calc(100dvh-3rem))] sm:w-[calc(100vw-3rem)] sm:max-w-[76rem]">
        <header className="shrink-0 border-b border-border px-5 py-3.5 pr-12">
          <DialogTitle className="text-base">Settings</DialogTitle>
          <DialogDescription className="sr-only">
            Signed in as {user.display_name}
          </DialogDescription>
        </header>

        <Tabs
          value={shownView}
          onValueChange={(next) => onViewChange(settingsView(next) ?? "general")}
          className="flex min-h-0 flex-1 flex-col overflow-hidden"
        >
          <div className="shrink-0 px-5 pt-3">
            <LinearTabsList ariaLabel="Settings sections">
              <LinearTab value="general">General</LinearTab>
              <LinearTab value="tokens">Tokens</LinearTab>
              <LinearTab value="compute">Compute</LinearTab>
              <LinearTab value="domains">Domains</LinearTab>
              {admin ? <LinearTab value="admin">Admin</LinearTab> : null}
            </LinearTabsList>
          </div>

          <TabsContent value="general" className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
            <div className="mx-auto max-w-4xl space-y-4">
              <AccountSettings />
              <BillingSettings planOpen={planOpen} onPlanOpenChange={setPlanOpen} />
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
            <ComputeSettings onUpgrade={openUpgrade} />
          </TabsContent>

          <TabsContent value="domains" className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
            <DomainSettings onUpgrade={openUpgrade} />
          </TabsContent>

          {admin ? (
            <TabsContent
              value="admin"
              className="flex min-h-0 flex-1 flex-col overflow-hidden px-5 py-4"
            >
              <AdminSettings />
            </TabsContent>
          ) : null}
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}
