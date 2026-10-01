import { useInfiniteQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { PanelError } from "@/components/shared/PanelError";
import { StubKindIcon } from "@/components/shared/StubKindIcon";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { PageFacts } from "@/components/shared/WorkspacePage/PageFacts";
import { Skeleton } from "@/components/ui/skeleton";
import { countLabel, formatKind } from "@/lib/format";
import { appsQueryOptions, selectApps, type App } from "@/lib/queries/apps";
import {
  containersQueryOptions,
  isRunningContainer,
  selectContainers,
  type Container,
} from "@/lib/queries/containers";
import {
  deploymentsQueryOptions,
  selectDeployments,
  type DeployedWorkload,
} from "@/lib/queries/deployments";
import { cn } from "@/lib/utils";
import { useWorkspace } from "@/lib/workspace-context";

import { AppCardActionsTrigger } from "./-components/AppCardActionsTrigger";
import { QuickstartEmptyState } from "./-components/QuickstartEmptyState";

export const Route = createFileRoute("/w/$workspace/apps/")({
  component: AppsPage,
  errorComponent: RouteErrorFallback,
});

/** What a card says about an app beyond its own record. */
type AppSummary = {
  latest: DeployedWorkload | undefined;
  kinds: Map<string, number>;
  runningContainers: number;
};

function AppsPage() {
  const { workspace } = useWorkspace();
  const apps = useInfiniteQuery(appsQueryOptions(workspace.name));
  // The newest deployment and the live containers of the workspace, read once
  // for every card rather than once per app.
  const deployments = useInfiniteQuery(deploymentsQueryOptions(workspace.name, { limit: 1000 }));
  const containers = useInfiniteQuery(containersQueryOptions(workspace.name, { live: true }));
  const list = selectApps(apps.data, apps.hasNextPage);
  const liveContainers = selectContainers(containers.data, false).items;
  const summaries = summarize(selectDeployments(deployments.data, false).items, liveContainers);
  const running = liveContainers.filter(isRunningContainer).length;

  return (
    <WorkspacePage
      title="Apps"
      pending={apps.isPending}
      description={
        // Gated on the data rather than on pending alone: reported as zeroes, a
        // failed read of the workspace reads as an empty workspace.
        apps.data ? (
          <PageFacts
            items={[
              countLabel(list.items.length, "app"),
              countLabel(
                list.items.reduce((total, app) => total + app.workloads, 0),
                "workload",
              ),
              containers.data ? countLabel(running, "running container") : null,
            ]}
          />
        ) : null
      }
      contentClassName="overflow-y-auto pr-1"
    >
      {apps.isPending ? (
        <AppCardsSkeleton />
      ) : apps.isError ? (
        <PanelError message={apps.error.message} layout="framed" />
      ) : list.items.length === 0 ? (
        <QuickstartEmptyState />
      ) : (
        <>
          <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-3" aria-label="Workspace apps">
            {list.items.map((app) => (
              <AppCard
                key={app.id}
                app={app}
                summary={summaries.get(app.name)}
                workspaceName={workspace.name}
              />
            ))}
          </section>
          <InfiniteScrollBoundary
            nextCursor={list.nextCursor}
            loading={apps.isFetchingNextPage}
            error={apps.isFetchNextPageError}
            onLoadMore={() => void apps.fetchNextPage()}
            resourceLabel="apps"
          />
        </>
      )}
    </WorkspacePage>
  );
}

function summarize(
  deployments: DeployedWorkload[],
  containers: Container[],
): Map<string, AppSummary> {
  const summaries = new Map<string, AppSummary>();
  const summary = (app: string) => {
    let found = summaries.get(app);
    if (!found) {
      found = { latest: undefined, kinds: new Map(), runningContainers: 0 };
      summaries.set(app, found);
    }
    return found;
  };
  for (const deployment of deployments) {
    const entry = summary(deployment.app);
    entry.kinds.set(deployment.kind, (entry.kinds.get(deployment.kind) ?? 0) + 1);
    if (!entry.latest || deployedAt(deployment) > deployedAt(entry.latest)) {
      entry.latest = deployment;
    }
  }
  for (const container of containers) {
    if (isRunningContainer(container)) summary(container.app).runningContainers += 1;
  }
  return summaries;
}

function deployedAt(deployment: DeployedWorkload): string {
  return deployment.deployed_at ?? deployment.created_at;
}

function AppCard({
  app,
  summary,
  workspaceName,
}: {
  app: App;
  summary: AppSummary | undefined;
  workspaceName: string;
}) {
  const latest = summary?.latest;
  const kinds = [...(summary?.kinds ?? new Map<string, number>())].sort(([left], [right]) =>
    left.localeCompare(right),
  );
  const active = app.state === "active";

  return (
    <article className="interactive-panel panel group relative min-w-0 rounded-md">
      <Link
        to="/w/$workspace/apps/$app"
        params={{ workspace: workspaceName, app: app.name }}
        aria-label={app.name}
        className="flex h-full min-h-[11rem] flex-col rounded-md p-4 outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
      >
        <header className="flex min-w-0 items-start gap-4 pr-8">
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 items-center gap-2">
              <h3 className="truncate text-base font-semibold text-foreground">{app.name}</h3>
              <span className="flex shrink-0 items-center gap-1.5 text-[11px] text-muted-foreground">
                <span
                  className={cn(
                    "size-1.5 rounded-full",
                    active ? "bg-positive" : "bg-muted-foreground",
                  )}
                  aria-hidden="true"
                />
                {active ? "Active" : "Paused"}
              </span>
            </div>
            <div className="mt-1.5 flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
              <span className="shrink-0">Latest workload</span>
              <span aria-hidden="true">·</span>
              {latest ? (
                <span className="flex min-w-0 items-center gap-1.5 text-foreground">
                  <StubKindIcon kind={latest.kind} className="size-3.5" />
                  <span className="mono truncate">{latest.name}</span>
                </span>
              ) : (
                <span>None</span>
              )}
            </div>
          </div>
        </header>

        <div className="mt-auto flex flex-wrap items-center gap-x-2 gap-y-1 pt-5 text-xs text-muted-foreground">
          <span>{countLabel(summary?.runningContainers ?? 0, "running container")}</span>
          <span aria-hidden="true">·</span>
          <span className="shrink-0">
            Deployed <LiveRelativeTime value={latest ? deployedAt(latest) : app.created_at} />
          </span>
        </div>

        <div className="mt-3 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5">
          {kinds.map(([kind, count]) => (
            <span key={kind} className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <StubKindIcon kind={kind} className="size-3.5" />
              <span>{formatKind(kind)}</span>
              <span className="mono tabular-nums text-foreground">{count}</span>
            </span>
          ))}
          {kinds.length === 0 ? <span className="text-xs text-muted-foreground">None</span> : null}
        </div>
      </Link>
      <div className="absolute right-3 top-3 z-10">
        <AppCardActionsTrigger app={app} workspace={workspaceName} />
      </div>
    </article>
  );
}

function AppCardsSkeleton() {
  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3" aria-hidden="true">
      {Array.from({ length: 6 }, (_, index) => (
        <div key={index} className="panel min-h-[11rem] rounded-md bg-card p-4">
          <div className="flex items-start justify-between gap-3">
            <div className="space-y-2">
              <Skeleton className="h-4 w-36" />
              <Skeleton className="h-3 w-48" />
            </div>
            <Skeleton className="size-4" />
          </div>
          <div className="mt-10 space-y-2">
            <Skeleton className="h-3 w-3/4" />
            <Skeleton className="h-3 w-2/3" />
          </div>
        </div>
      ))}
    </div>
  );
}
