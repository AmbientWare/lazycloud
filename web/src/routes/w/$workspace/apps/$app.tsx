import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { createFileRoute, Outlet } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { PanelError } from "@/components/shared/PanelError";
import type { Deployment } from "@/lib/api/schemas";
import { appQueryOptions } from "@/lib/queries/apps";
import { containersQueryOptions, selectContainerList } from "@/lib/queries/containers";
import { deploymentsInfiniteQueryOptions, selectDeploymentList } from "@/lib/queries/deployments";
import { sandboxesQueryOptions } from "@/lib/queries/sandboxes";
import { taskBucketsQueryOptions, tasksQueryOptions } from "@/lib/queries/tasks";
import { useWorkspace } from "@/lib/workspace-context";

import { AppActivitySection } from "./-components/AppActivitySection";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { Skeleton } from "@/components/ui/skeleton";

import { AppDetailFacts } from "./-components/AppDetailHeader";
import { AppLifecycleActions } from "./-components/AppLifecycleActions";
import { AppRecentTasksSection } from "./-components/AppRecentTasksSection";
import { AppSandboxesSection } from "./-components/AppSandboxesSection";
import { AppWorkloadsSection } from "./-components/AppWorkloadsSection";
import { groupDeploymentsByWorkload } from "./-workloads/grouping";

export const Route = createFileRoute("/w/$workspace/apps/$app")({
  component: AppDetailPage,
  errorComponent: RouteErrorFallback,
});

function AppDetailPage() {
  const { app } = Route.useParams();
  const { workspace } = useWorkspace();
  const appRecord = useQuery(appQueryOptions(workspace.id, app));
  const deployments = useInfiniteQuery(
    deploymentsInfiniteQueryOptions(workspace.id, { appId: app }),
  );
  const containers = useInfiniteQuery(containersQueryOptions(workspace.name, { app }));
  const activity = useQuery(taskBucketsQueryOptions(workspace.name, 3600, app));
  const tasks = useQuery(tasksQueryOptions(workspace.name, { app, root_only: true }, 15));
  const sandboxes = useQuery(sandboxesQueryOptions(workspace.id, { limit: 50, appId: app }));

  const deploymentList = selectDeploymentList(deployments.data, deployments.hasNextPage);
  const deploymentRows = deploymentList.items;
  const workloadGroups = groupDeploymentsByWorkload(deploymentRows, app);
  const activeWorkloads = workloadGroups.filter((group) => group.active).length;
  const latestDeployment = newestDeployment(deploymentRows, app);
  const containerList = selectContainerList(containers.data, containers.hasNextPage);
  const runningContainers = containerList.items.filter(
    (container) => container.state === "ready",
  ).length;
  const continuingDeployments = Boolean(deploymentList.nextCursor);
  const continuationCursor = continuingDeployments
    ? `deployments:${deploymentList.nextCursor}`
    : containerList.nextCursor
      ? `containers:${containerList.nextCursor}`
      : undefined;

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <WorkspacePage
        title={
          appRecord.data ? (
            appRecord.data.name
          ) : (
            <Skeleton className="h-6 w-48" aria-hidden="true" />
          )
        }
        description={
          appRecord.data && deployments.data ? (
            <AppDetailFacts
              latestDeployment={latestDeployment}
              workloadCount={workloadGroups.length}
              activeWorkloads={activeWorkloads}
            />
          ) : null
        }
        actions={
          appRecord.data ? (
            <AppLifecycleActions
              app={appRecord.data}
              workspaceId={workspace.id}
              workspaceName={workspace.name}
            />
          ) : null
        }
        contentClassName="overflow-y-auto lg:overflow-hidden"
      >
        {appRecord.isError ? (
          <PanelError message={appRecord.error.message} layout="framed" />
        ) : (
          <div className="grid min-h-full gap-3 lg:h-full lg:min-h-0 lg:grid-cols-[minmax(0,1.45fr)_minmax(19rem,0.8fr)] lg:overflow-hidden">
            <AppWorkloadsSection
              workspaceId={workspace.id}
              workspaceName={workspace.name}
              app={app}
              deployments={deploymentRows}
              containers={containerList.items}
              pending={deployments.isPending || containers.isPending}
              error={
                queryError(deployments.isFetchNextPageError ? null : deployments.error) ??
                queryError(containers.isFetchNextPageError ? null : containers.error)
              }
              nextCursor={continuationCursor}
              loadingMore={deployments.isFetchingNextPage || containers.isFetchingNextPage}
              loadMoreError={deployments.isFetchNextPageError || containers.isFetchNextPageError}
              onLoadMore={() => {
                if (continuingDeployments) void deployments.fetchNextPage();
                else void containers.fetchNextPage();
              }}
              continuationLabel={continuingDeployments ? "workloads" : "container state"}
            />
            <div className="grid min-h-0 gap-3 lg:grid-rows-[minmax(7rem,0.8fr)_minmax(10rem,1.25fr)_minmax(7rem,0.9fr)] lg:overflow-hidden">
              <AppActivitySection
                buckets={activity.data}
                runningContainers={runningContainers}
                pending={activity.isPending}
                error={queryError(activity.error)}
              />
              <AppRecentTasksSection
                workspaceName={workspace.name}
                app={app}
                tasks={tasks.data}
                pending={tasks.isPending}
                error={queryError(tasks.error)}
              />
              <AppSandboxesSection
                workspaceName={workspace.name}
                sandboxes={sandboxes.data?.data}
                pending={sandboxes.isPending}
                error={queryError(sandboxes.error)}
              />
            </div>
          </div>
        )}
      </WorkspacePage>
      <Outlet />
    </div>
  );
}

function newestDeployment(
  deployments: Deployment[] | undefined,
  app: string,
): Deployment | undefined {
  return (deployments ?? [])
    .filter((deployment) => deployment.app_id === app)
    .slice()
    .sort((left, right) => right.created_at.localeCompare(left.created_at))[0];
}

function queryError(error: Error | null): string | undefined {
  return error?.message;
}
