import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { createFileRoute, Outlet } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { PanelError } from "@/components/shared/PanelError";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { Skeleton } from "@/components/ui/skeleton";
import { appQueryOptions } from "@/lib/queries/apps";
import {
  containersQueryOptions,
  isRunningContainer,
  selectContainers,
} from "@/lib/queries/containers";
import {
  deploymentsQueryOptions,
  selectDeployments,
  type DeployedWorkload,
} from "@/lib/queries/deployments";
import { taskBucketsQueryOptions } from "@/lib/queries/metrics";
import { sandboxesQueryOptions } from "@/lib/queries/sandboxes";
import { tasksQueryOptions } from "@/lib/queries/tasks";
import { useWorkspace } from "@/lib/workspace-context";

import { AppActivitySection } from "./-components/AppActivitySection";
import { AppDetailFacts } from "./-components/AppDetailHeader";
import { AppLifecycleActions } from "./-components/AppLifecycleActions";
import { AppRecentTasksSection } from "./-components/AppRecentTasksSection";
import { AppSandboxesSection } from "./-components/AppSandboxesSection";
import { AppWorkloadsSection } from "./-components/AppWorkloadsSection";

export const Route = createFileRoute("/w/$workspace/apps/$app")({
  component: AppDetailPage,
  errorComponent: RouteErrorFallback,
});

function AppDetailPage() {
  const { app: appName } = Route.useParams();
  const { workspace } = useWorkspace();
  const app = useQuery(appQueryOptions(workspace.name, appName));
  const deployments = useInfiniteQuery(deploymentsQueryOptions(workspace.name, { app: appName }));
  const containers = useInfiniteQuery(containersQueryOptions(workspace.name, { live: true }));
  const activity = useQuery(taskBucketsQueryOptions(workspace.name, 3600, { app: appName }));
  const tasks = useQuery(tasksQueryOptions(workspace.name, { limit: 15, app: appName }));
  const sandboxes = useQuery(sandboxesQueryOptions(workspace.name, { limit: 50, appId: appName }));

  const deploymentList = selectDeployments(deployments.data, deployments.hasNextPage);
  const workloads = deploymentList.items;
  const activeWorkloads = workloads.filter((workload) => workload.state === "active").length;
  const appContainers = selectContainers(containers.data, false).items.filter(
    (container) => container.app === appName,
  );
  const runningContainers = appContainers.filter(isRunningContainer).length;

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <WorkspacePage
        title={app.data ? app.data.name : <Skeleton className="h-6 w-48" aria-hidden="true" />}
        description={
          app.data && deployments.data ? (
            <AppDetailFacts
              latestDeployment={newestDeployment(workloads)}
              workloadCount={workloads.length}
              activeWorkloads={activeWorkloads}
            />
          ) : null
        }
        actions={
          app.data ? <AppLifecycleActions app={app.data} workspace={workspace.name} /> : null
        }
        contentClassName="overflow-y-auto lg:overflow-hidden"
      >
        {app.isError ? (
          <PanelError message={app.error.message} layout="framed" />
        ) : (
          <div className="grid min-h-full gap-3 lg:h-full lg:min-h-0 lg:grid-cols-[minmax(0,1.45fr)_minmax(19rem,0.8fr)] lg:overflow-hidden">
            <AppWorkloadsSection
              workspace={workspace.name}
              app={appName}
              deployments={workloads}
              containers={appContainers}
              pending={deployments.isPending || containers.isPending}
              error={
                (deployments.isFetchNextPageError ? null : deployments.error)?.message ??
                containers.error?.message
              }
              nextCursor={deploymentList.nextCursor}
              loadingMore={deployments.isFetchingNextPage}
              loadMoreError={deployments.isFetchNextPageError}
              onLoadMore={() => void deployments.fetchNextPage()}
            />
            <div className="grid min-h-0 gap-3 lg:grid-rows-[minmax(7rem,0.8fr)_minmax(10rem,1.25fr)_minmax(7rem,0.9fr)] lg:overflow-hidden">
              <AppActivitySection
                buckets={activity.data?.items}
                runningContainers={runningContainers}
                pending={activity.isPending}
                error={activity.error?.message}
              />
              <AppRecentTasksSection
                workspaceName={workspace.name}
                app={appName}
                tasks={tasks.data?.tasks}
                pending={tasks.isPending}
                error={tasks.error?.message}
              />
              <AppSandboxesSection
                workspaceName={workspace.name}
                sandboxes={sandboxes.data?.data}
                pending={sandboxes.isPending}
                error={sandboxes.error?.message}
              />
            </div>
          </div>
        )}
      </WorkspacePage>
      <Outlet />
    </div>
  );
}

function newestDeployment(workloads: DeployedWorkload[]): DeployedWorkload | undefined {
  const deployedAt = (workload: DeployedWorkload) => workload.deployed_at ?? workload.created_at;
  return [...workloads].sort((left, right) => deployedAt(right).localeCompare(deployedAt(left)))[0];
}
