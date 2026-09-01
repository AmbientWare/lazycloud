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

export const Route = createFileRoute("/w/$workspace/apps/$appId")({
  component: AppDetailPage,
  errorComponent: RouteErrorFallback,
});

function AppDetailPage() {
  const { appId } = Route.useParams();
  const { workspace } = useWorkspace();
  const app = useQuery(appQueryOptions(workspace.id, appId));
  const deployments = useInfiniteQuery(deploymentsInfiniteQueryOptions(workspace.id, { appId }));
  const containers = useInfiniteQuery(containersQueryOptions(workspace.id, { appId }));
  const activity = useQuery(taskBucketsQueryOptions(workspace.id, 3600, { appId }));
  const tasks = useQuery(tasksQueryOptions(workspace.id, { limit: 15, appId, rootOnly: true }));
  const sandboxes = useQuery(sandboxesQueryOptions(workspace.id, { limit: 50, appId }));

  const deploymentList = selectDeploymentList(deployments.data, deployments.hasNextPage);
  const deploymentRows = deploymentList.items;
  const workloadGroups = groupDeploymentsByWorkload(deploymentRows, appId);
  const activeWorkloads = workloadGroups.filter((group) => group.active).length;
  const latestDeployment = newestDeployment(deploymentRows, appId);
  const containerList = selectContainerList(containers.data, containers.hasNextPage);
  const runningContainers = containerList.items.filter(
    (item) => item.container.status === "running",
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
        title={app.data ? app.data.name : <Skeleton className="h-6 w-48" aria-hidden="true" />}
        description={
          app.data && deployments.data ? (
            <AppDetailFacts
              latestDeployment={latestDeployment}
              workloadCount={workloadGroups.length}
              activeWorkloads={activeWorkloads}
            />
          ) : null
        }
        actions={
          app.data ? (
            <AppLifecycleActions
              app={app.data}
              workspaceId={workspace.id}
              workspaceName={workspace.name}
            />
          ) : null
        }
        contentClassName="overflow-y-auto lg:overflow-hidden"
      >
        {app.isError ? (
          <PanelError message={app.error.message} layout="framed" />
        ) : (
          <div className="grid min-h-full gap-3 lg:h-full lg:min-h-0 lg:grid-cols-[minmax(0,1.45fr)_minmax(19rem,0.8fr)] lg:overflow-hidden">
            <AppWorkloadsSection
              workspaceName={workspace.name}
              appId={appId}
              deployments={deploymentRows}
              containers={containerList.items.map((item) => item.container)}
              pending={deployments.isPending || containers.isPending}
              error={queryError(deployments.error) ?? queryError(containers.error)}
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
                buckets={activity.data?.items}
                runningContainers={runningContainers}
                pending={activity.isPending}
                error={queryError(activity.error)}
              />
              <AppRecentTasksSection
                workspaceName={workspace.name}
                appId={appId}
                tasks={tasks.data?.data}
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
  appId: string,
): Deployment | undefined {
  return (deployments ?? [])
    .filter((deployment) => deployment.app_id === appId)
    .slice()
    .sort((left, right) => right.created_at.localeCompare(left.created_at))[0];
}

function queryError(error: Error | null): string | undefined {
  return error?.message;
}
