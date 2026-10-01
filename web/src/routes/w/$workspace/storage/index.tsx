import { useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { Plus } from "lucide-react";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { LinearTab, LinearTabsList } from "@/components/shared/LinearSelect";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { PageFacts } from "@/components/shared/WorkspacePage/PageFacts";
import { Button } from "@/components/ui/button";
import { CollectionAccordion } from "./-components/CollectionAccordion";
import { DisksTab } from "./-components/DisksTab";
import { SecretsTab } from "./-components/SecretsTab";
import { VolumesTab } from "./-components/VolumesTab";
import { Artifacts } from "@/components/shared/Artifacts";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { countLabel } from "@/lib/format";
import {
  secretsQueryOptions,
  selectSecrets,
  selectVolumes,
  volumesQueryOptions,
} from "@/lib/queries/storage";
import { useWorkspace } from "@/lib/workspace-context";

const STORAGE_TABS = [
  { key: "volumes", title: "Volumes" },
  { key: "disks", title: "Disks" },
  { key: "artifacts", title: "Artifacts" },
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
      typeof search.view === "string" && STORAGE_TABS.some((tab) => tab.key === search.view)
        ? (search.view as StorageTab)
        : "volumes",
  }),
  component: StoragePage,
  errorComponent: RouteErrorFallback,
});

function StoragePage() {
  const { workspace } = useWorkspace();
  const volumes = useInfiniteQuery(volumesQueryOptions(workspace.name));
  const secrets = useInfiniteQuery(secretsQueryOptions(workspace.name));
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  const [creating, setCreating] = useState<Exclude<StorageTab, "artifacts" | "disks"> | null>(null);
  const createView = search.view === "artifacts" || search.view === "disks" ? null : search.view;
  const createLabel =
    createView === "volumes"
      ? "New volume"
      : createView === "secrets"
        ? "New secret"
        : createView === "maps"
          ? "New map"
          : "New queue";

  return (
    <WorkspacePage
      title="Storage"
      description={
        volumes.data || secrets.data ? (
          <PageFacts
            items={[
              volumes.data
                ? countLabel(selectVolumes(volumes.data, false).items.length, "volume")
                : null,
              secrets.data
                ? countLabel(selectSecrets(secrets.data, false).items.length, "secret")
                : null,
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
          value={search.view}
          onValueChange={(view) => {
            setCreating(null);
            void navigate({ search: { view: view as StorageTab }, replace: true });
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
              workspaceId={workspace.name}
              workspaceName={workspace.name}
              creating={creating === "volumes"}
              onCreatingChange={(open) => setCreating(open ? "volumes" : null)}
            />
          </TabsContent>
          <TabsContent value="disks" className="min-h-0 flex-1 overflow-hidden">
            <DisksTab key={workspace.name} workspaceId={workspace.name} />
          </TabsContent>
          <TabsContent
            value="secrets"
            className="min-h-0 flex-1 overflow-y-auto lg:overflow-hidden"
          >
            <SecretsTab
              workspaceId={workspace.name}
              workspaceName={workspace.name}
              creating={creating === "secrets"}
              onCreatingChange={(open) => setCreating(open ? "secrets" : null)}
            />
          </TabsContent>
          {(["queues", "maps"] as const).map((kind) => (
            <TabsContent
              key={kind}
              value={kind}
              className="min-h-0 flex-1 overflow-y-auto lg:overflow-hidden"
            >
              <CollectionAccordion
                key={`${workspace.name}:${kind}`}
                kind={kind}
                workspaceId={workspace.name}
                creating={creating === kind}
                onCreatingChange={(open) => setCreating(open ? kind : null)}
              />
            </TabsContent>
          ))}
          <TabsContent value="artifacts" className="min-h-0 flex-1 overflow-hidden">
            <Artifacts key={workspace.name} workspaceId={workspace.name} />
          </TabsContent>
        </Tabs>
      </section>
    </WorkspacePage>
  );
}
