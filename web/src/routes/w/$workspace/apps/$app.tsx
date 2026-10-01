import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Outlet } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { PanelError } from "@/components/shared/PanelError";
import { appActivityQueryOptions, appQueryOptions } from "@/lib/queries/apps";
import { deployedAt, deploymentsQueryOptions, workloadRunning } from "@/lib/queries/deployments";
import { sandboxesQueryOptions } from "@/lib/queries/sandboxes";
import { tasksQueryOptions } from "@/lib/queries/tasks";
import { useWorkspace } from "@/lib/workspace-context";

import { AppActivitySection } from "./-components/AppActivitySection";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { Skeleton } from "@/components/ui/skeleton";

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
  const { app } = Route.useParams();
  const { workspace } = useWorkspace();
  const appRecord = useQuery(appQueryOptions(workspace.name, app));
  const workloads = useQuery(deploymentsQueryOptions(workspace.name, app));
  const activity = useQuery(appActivityQueryOptions(workspace.name, app));
  const tasks = useQuery(tasksQueryOptions(workspace.name, { app, root_only: true }, 15));
  const sandboxes = useQuery(sandboxesQueryOptions(workspace.id, { limit: 50, appId: app }));

  const latest = workloads.data?.[0];

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
          appRecord.data && workloads.data ? (
            <AppDetailFacts
              lastDeployedAt={latest ? deployedAt(latest) : undefined}
              workloadCount={appRecord.data.workloads}
              activeWorkloads={workloads.data.filter(workloadRunning).length}
            />
          ) : null
        }
        actions={
          appRecord.data ? (
            <AppLifecycleActions app={appRecord.data} workspace={workspace.name} />
          ) : null
        }
        contentClassName="overflow-y-auto lg:overflow-hidden"
      >
        {appRecord.isError ? (
          <PanelError message={appRecord.error.message} layout="framed" />
        ) : (
          <div className="grid min-h-full gap-3 lg:h-full lg:min-h-0 lg:grid-cols-[minmax(0,1.45fr)_minmax(19rem,0.8fr)] lg:overflow-hidden">
            <AppWorkloadsSection
              workspace={workspace.name}
              app={app}
              workloads={workloads.data}
              pending={workloads.isPending}
              error={workloads.error?.message}
            />
            <div className="grid min-h-0 gap-3 lg:grid-rows-[minmax(7rem,0.8fr)_minmax(10rem,1.25fr)_minmax(7rem,0.9fr)] lg:overflow-hidden">
              <AppActivitySection
                series={activity.data?.series}
                runningContainers={appRecord.data?.running_containers}
                pending={activity.isPending}
                error={activity.error?.message}
              />
              <AppRecentTasksSection
                workspaceName={workspace.name}
                app={app}
                tasks={tasks.data}
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
