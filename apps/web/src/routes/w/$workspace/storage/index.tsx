import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { Plus } from "lucide-react";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { LinearTab, LinearTabsList } from "@/components/shared/LinearSelect";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { PageFacts } from "@/components/shared/WorkspacePage/PageFacts";
import { Button } from "@/components/ui/button";
import { CollectionAccordion } from "./-components/CollectionAccordion";
import { SecretsTab } from "./-components/SecretsTab";
import { VolumesTab } from "./-components/VolumesTab";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { collectionResources } from "@/lib/api/resources";
import { countLabel } from "@/lib/format";
import { secretsQueryOptions, volumesQueryOptions } from "@/lib/queries/storage";
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
    view: STORAGE_TABS.find((tab) => tab.key === search.view)?.key ?? "volumes",
  }),
  component: StoragePage,
  errorComponent: RouteErrorFallback,
});

function StoragePage() {
  const { workspace } = useWorkspace();
  const volumes = useQuery(volumesQueryOptions(workspace.id));
  const secrets = useQuery(secretsQueryOptions(workspace.id));
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  const [creating, setCreating] = useState<"volumes" | "secrets" | null>(null);
  const createView = search.view === "volumes" || search.view === "secrets" ? search.view : null;
  const createLabel = createView === "volumes" ? "New volume" : "New secret";

  return (
    <WorkspacePage
      title="Storage"
      description={
        volumes.data || secrets.data ? (
          <PageFacts
            items={[
              volumes.data ? countLabel(volumes.data.volumes.length, "volume") : null,
              secrets.data ? countLabel(secrets.data.secrets.length, "secret") : null,
            ]}
          />
        ) : null
      }
    >
      {/* The tab strip is the card's header row, the way the tasks toolbar is:
          the tabs and the button that acts on the selected one belong to the
          thing they are steering, not to the space above it. */}
      <section className="panel flex h-full min-h-0 flex-col overflow-hidden rounded-md">
        <Tabs
          key={workspace.id}
          value={search.view}
          onValueChange={(view) => {
            setCreating(null);
            const next = STORAGE_TABS.find((tab) => tab.key === view)?.key;
            if (next) void navigate({ search: { view: next }, replace: true });
          }}
          className="flex h-full min-h-0 w-full flex-col overflow-hidden"
        >
          <LinearTabsList
            ariaLabel="Storage resources"
            className="px-3 py-2"
            listClassName="sm:flex-none"
            action={
              createView ? (
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
              ) : null
            }
          >
            {STORAGE_TABS.map((tab) => (
              <LinearTab key={tab.key} value={tab.key}>
                {tab.title}
              </LinearTab>
            ))}
          </LinearTabsList>

          <TabsContent
            value="volumes"
            className="min-h-0 flex-1 overflow-y-auto lg:overflow-hidden"
          >
            <VolumesTab
              workspaceId={workspace.id}
              workspaceName={workspace.name}
              creating={creating === "volumes"}
              onCreatingChange={(open) => setCreating(open ? "volumes" : null)}
            />
          </TabsContent>
          <TabsContent
            value="secrets"
            className="min-h-0 flex-1 overflow-y-auto lg:overflow-hidden"
          >
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
              className="min-h-0 flex-1 overflow-y-auto lg:overflow-hidden"
            >
              <CollectionAccordion config={config} workspaceId={workspace.id} />
            </TabsContent>
          ))}
        </Tabs>
      </section>
    </WorkspacePage>
  );
}
