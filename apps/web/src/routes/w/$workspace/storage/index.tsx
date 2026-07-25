import { useState } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { Plus } from "lucide-react";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { LinearTab, LinearTabsList } from "@/components/shared/LinearSelect";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { Button } from "@/components/ui/button";
import { CollectionAccordion } from "./-components/CollectionAccordion";
import { SecretsTab } from "./-components/SecretsTab";
import { VolumesTab } from "./-components/VolumesTab";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { collectionResources } from "@/lib/api/resources";
import { useWorkspace } from "@/lib/workspace-context";

const STORAGE_TABS = [
  { key: "volumes", title: "Volumes" },
  { key: "secrets", title: "Secrets" },
  { key: "queues", title: "Queues" },
  { key: "maps", title: "Maps" },
] as const;
type StorageTab = (typeof STORAGE_TABS)[number]["key"];

type StorageSearch = {
  view: StorageTab;
};

export const Route = createFileRoute("/w/$workspace/storage/")({
  validateSearch: (search: Record<string, unknown>): StorageSearch => ({
    view:
      typeof search.view === "string" &&
      STORAGE_TABS.some((tab) => tab.key === search.view)
        ? (search.view as StorageTab)
        : "volumes",
  }),
  component: StoragePage,
  errorComponent: RouteErrorFallback,
});

function StoragePage() {
  const { workspace } = useWorkspace();
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  const [creating, setCreating] = useState<"volumes" | "secrets" | null>(null);
  const createView =
    search.view === "volumes" || search.view === "secrets" ? search.view : null;
  const createLabel = createView === "volumes" ? "New volume" : "New secret";

  return (
    <WorkspacePage title="Storage">
      <Tabs
        value={search.view}
        onValueChange={(view) => {
          setCreating(null);
          void navigate({ search: { view: view as StorageTab }, replace: true });
        }}
        className="flex h-full min-h-0 w-full flex-col overflow-hidden"
      >
        <LinearTabsList
          ariaLabel="Storage resources"
          listClassName="sm:flex-none"
          action={createView ? (
            <Button
              size="sm"
              className="px-2 sm:px-3"
              aria-label={createLabel}
              title={createLabel}
              disabled={creating === createView}
              onClick={() => setCreating(createView)}
            >
              <Plus />
              <span className="hidden sm:inline">{createLabel}</span>
            </Button>
          ) : null}
        >
          {STORAGE_TABS.map((tab) => (
            <LinearTab key={tab.key} value={tab.key}>
              {tab.title}
            </LinearTab>
          ))}
        </LinearTabsList>

        <TabsContent value="volumes" className="mt-3 min-h-0 flex-1 overflow-y-auto lg:overflow-hidden">
          <VolumesTab
            workspaceId={workspace.id}
            workspaceName={workspace.name}
            creating={creating === "volumes"}
            onCreatingChange={(open) => setCreating(open ? "volumes" : null)}
          />
        </TabsContent>
        <TabsContent value="secrets" className="mt-3 min-h-0 flex-1 overflow-y-auto lg:overflow-hidden">
          <SecretsTab
            workspaceId={workspace.id}
            workspaceName={workspace.name}
            creating={creating === "secrets"}
            onCreatingChange={(open) => setCreating(open ? "secrets" : null)}
          />
        </TabsContent>
        {collectionResources.map((config) => (
          <TabsContent
            key={config.key}
            value={config.key}
            className="mt-3 min-h-0 flex-1 overflow-y-auto lg:overflow-hidden"
          >
            <CollectionAccordion config={config} workspaceId={workspace.id} />
          </TabsContent>
        ))}
      </Tabs>
    </WorkspacePage>
  );
}
