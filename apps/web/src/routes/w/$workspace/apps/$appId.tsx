import { useState } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { createFileRoute, Outlet } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { PanelError } from "@/components/shared/PanelError";
import { appQueryOptions, appSummariesQueryOptions } from "@/lib/queries/apps";
import { workloadsInfiniteQueryOptions } from "@/lib/queries/deployments";
import { selectInfiniteList } from "@/lib/queries/infinite-list";
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

export const Route = createFileRoute("/w/$workspace/apps/$appId")({
  component: AppDetailPage,
  errorComponent: RouteErrorFallback,
});

function AppDetailPage() {
  const { appId } = Route.useParams();
  const { workspace } = useWorkspace();
  const [kind, setKind] = useState<string>();
  const app = useQuery(appQueryOptions(workspace.id, appId));
  const workloads = useInfiniteQuery(workloadsInfiniteQueryOptions(workspace.id, appId, kind));
  const summaries = useQuery(appSummariesQueryOptions(workspace.id));
  const activity = useQuery(taskBucketsQueryOptions(workspace.id, 3600, { appId }));
  const tasks = useQuery(tasksQueryOptions(workspace.id, { limit: 15, appId, rootOnly: true }));
  const sandboxes = useQuery(sandboxesQueryOptions(workspace.id, { limit: 50, appId }));

  const workloadList = selectInfiniteList(
    workloads.data,
    workloads.hasNextPage,
    (item) => `${item.deployment.kind}:${item.deployment.name}`,
  );
  const summary = summaries.data?.items.find((item) => item.app.id === appId);

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <WorkspacePage
        title={app.data ? app.data.name : <Skeleton className="h-6 w-48" aria-hidden="true" />}
        description={
          summary ? (
            <AppDetailFacts
              latestDeployment={summary.latest_deployment ?? undefined}
              workloadCount={summary.workload_count}
              activeWorkloads={summary.active_versions}
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
              workspaceId={workspace.id}
              workspaceName={workspace.name}
              appId={appId}
              workloads={workloadList.items}
              kind={kind}
              onKindChange={setKind}
              kinds={Object.keys(summary?.workload_kinds ?? {}).sort()}
              pending={workloads.isPending}
              error={workloads.isError && !workloads.data ? workloads.error.message : undefined}
              nextCursor={workloadList.nextCursor}
              loadingMore={workloads.isFetchingNextPage}
              loadMoreError={workloads.isFetchNextPageError}
              onLoadMore={() => void workloads.fetchNextPage()}
            />
            <div className="grid min-h-0 gap-3 lg:grid-rows-[minmax(7rem,0.8fr)_minmax(10rem,1.25fr)_minmax(7rem,0.9fr)] lg:overflow-hidden">
              <AppActivitySection
                buckets={activity.data?.items}
                runningContainers={summary?.running_containers}
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

function queryError(error: Error | null): string | undefined {
  return error?.message;
}
