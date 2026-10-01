import { createFileRoute, Link, Outlet } from "@tanstack/react-router";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";

import { PanelErrorBoundary, RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { LinearTab, LinearTabsList } from "@/components/shared/LinearSelect";
import { Panel } from "@/components/shared/Panel";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { StubKindIcon } from "@/components/shared/StubKindIcon";
import { TaskTable } from "@/components/shared/TaskTable";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { PageFacts } from "@/components/shared/WorkspacePage/PageFacts";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { isApiError } from "@/lib/api/client";
import { countLabel } from "@/lib/format";
import {
  containersQueryOptions,
  isRunningContainer,
  selectContainers,
} from "@/lib/queries/containers";
import {
  deploymentsQueryOptions,
  functionQueryOptions,
  selectDeployments,
  type DeployedWorkload,
} from "@/lib/queries/deployments";
import { taskLatencyQueryOptions } from "@/lib/queries/metrics";
import { tasksQueryOptions } from "@/lib/queries/tasks";
import { useWorkspace } from "@/lib/workspace-context";

import { CallMethods } from "./-workloads/CallMethods";
import { LatencyPanel, latencyHasSignal } from "./-workloads/LatencyPanel";
import { Playground } from "./-workloads/Playground";
import { functionManifest, type DeploymentManifest } from "./-workloads/playground-form";
import { VersionHistory } from "./-workloads/VersionHistory";
import { WorkloadConfiguration } from "./-workloads/WorkloadConfiguration";
import { WorkloadOperation } from "./-workloads/WorkloadOperation";

export const Route = createFileRoute("/w/$workspace/apps/$app_/workloads/$kind/$name")({
  component: WorkloadDetailRoute,
  errorComponent: RouteErrorFallback,
});

function WorkloadDetailRoute() {
  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <WorkloadDetailPage />
      <Outlet />
    </div>
  );
}

function WorkloadDetailPage() {
  const { app, kind, name } = Route.useParams();
  const { workspace } = useWorkspace();
  const fn = useQuery({
    ...functionQueryOptions(workspace.name, app, name),
    enabled: kind === "function",
  });
  const deployments = useInfiniteQuery(deploymentsQueryOptions(workspace.name, { app, name }));
  const containers = useInfiniteQuery(containersQueryOptions(workspace.name, { live: true }));

  if (kind !== "function") {
    return (
      <PanelEmpty message={`No deployed workload named ${name} in this app`} className="h-full" />
    );
  }
  if (fn.isPending || deployments.isPending) return <WorkloadSkeleton />;
  if (fn.isError && isApiError(fn.error, 404)) {
    return (
      <PanelEmpty message={`No deployed workload named ${name} in this app`} className="h-full" />
    );
  }
  const loadError = fn.error ?? deployments.error;
  if (loadError) return <PanelError message={loadError.message} />;
  const workload = selectDeployments(deployments.data, false).items.find(
    (item) => item.name === name,
  );
  if (!fn.data || !workload) {
    return (
      <PanelEmpty message={`No deployed workload named ${name} in this app`} className="h-full" />
    );
  }

  const resource = functionManifest(fn.data);
  const running = selectContainers(containers.data, false).items.filter(
    (container) =>
      container.app === app && container.function === name && isRunningContainer(container),
  );
  const active = workload.state === "active" && workload.app_state !== "paused";
  const scheduled = Boolean(fn.data.schedule);
  const backLink = (
    <Link
      to="/w/$workspace/apps/$app"
      params={{ workspace: workspace.name, app }}
      className="flex h-8 min-w-0 items-center gap-1.5 rounded-md border border-input bg-card px-2.5 text-xs text-muted-foreground outline-none transition-colors hover:border-brand/40 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
    >
      <ArrowLeft className="size-3.5 shrink-0" aria-hidden="true" />
      {app}
    </Link>
  );

  return (
    <WorkspacePage
      title={<span className="mono">{name}</span>}
      description={
        <PageFacts
          items={[
            <span key="kind" className="flex items-center gap-1.5">
              <StubKindIcon kind={kind} className="size-3.5" />
              {/* A scheduled function still reads as a schedule here: it is what
                  the person looking at the list is scanning for. */}
              {scheduled ? "Schedule" : "Function"}
            </span>,
            <span key="version" className="mono">
              v{workload.version ?? fn.data.active_release.version}
            </span>,
            "Token required",
            containers.data ? countLabel(running.length, "running", "running") : null,
          ]}
        />
      }
      actions={backLink}
      headerDetails={<WorkloadOperation fn={fn.data} resource={resource} active={active} />}
      contentClassName="flex flex-col gap-3 overflow-y-auto xl:grid xl:grid-cols-[minmax(20rem,2fr)_minmax(0,3fr)] xl:overflow-hidden"
    >
      <Tabs
        key={JSON.stringify([app, kind, name])}
        defaultValue={active && !scheduled ? "invoke" : "versions"}
        className="panel flex shrink-0 flex-col overflow-hidden rounded-md xl:min-h-0"
      >
        <LinearTabsList
          ariaLabel="Workload inspector views"
          className="min-h-11 shrink-0 bg-card px-2"
        >
          {active ? <LinearTab value="invoke">Invoke</LinearTab> : null}
          <LinearTab value="versions">Versions</LinearTab>
          <LinearTab value="configuration">Configuration</LinearTab>
          <LinearTab value="call">Call</LinearTab>
        </LinearTabsList>

        {active ? (
          <TabsContent value="invoke" className="m-0 min-h-0 flex-1 overflow-hidden">
            <PanelErrorBoundary title="Invoke could not be displayed">
              <Playground workspace={workspace.name} resource={resource} />
            </PanelErrorBoundary>
          </TabsContent>
        ) : null}

        <TabsContent value="versions" className="m-0 min-h-0 flex-1 overflow-auto max-xl:flex-none">
          <VersionHistory workload={workload} workspace={workspace.name} />
        </TabsContent>

        <TabsContent
          value="configuration"
          className="m-0 min-h-0 flex-1 overflow-auto max-xl:flex-none"
        >
          <WorkloadConfiguration spec={fn.data.active_release.spec} />
        </TabsContent>
        <TabsContent value="call" className="m-0 min-h-0 flex-1 overflow-auto max-xl:flex-none">
          <PanelErrorBoundary title="Call methods could not be displayed">
            <CallMethods workspaceName={workspace.name} resource={resource} />
          </PanelErrorBoundary>
        </TabsContent>
      </Tabs>

      <Panel
        title="Activity"
        contentClassName="flex flex-col overflow-hidden p-0"
        className="min-h-[24rem] shrink-0 xl:min-h-0"
      >
        <WorkloadLatency workspace={workspace.name} resource={resource} />
        <WorkloadRuns workspace={workspace.name} workload={workload} />
      </Panel>
    </WorkspacePage>
  );
}

function WorkloadLatency({
  workspace,
  resource,
}: {
  workspace: string;
  resource: DeploymentManifest;
}) {
  const latency = useQuery(taskLatencyQueryOptions(workspace, resource.app, resource.name));
  if (!(latency.isPending || latency.isError || latencyHasSignal(latency.data?.buckets))) {
    return null;
  }
  return (
    <div className="h-52 shrink-0 border-b border-border/80 p-3">
      <PanelErrorBoundary title="Performance could not be displayed">
        <LatencyPanel
          buckets={latency.data?.buckets}
          pending={latency.isPending}
          error={latency.error}
          kind={resource.kind}
        />
      </PanelErrorBoundary>
    </div>
  );
}

function WorkloadRuns({ workspace, workload }: { workspace: string; workload: DeployedWorkload }) {
  const tasks = useQuery(
    tasksQueryOptions(workspace, { limit: 50, app: workload.app, function: workload.name }),
  );
  if (tasks.isError) {
    return <PanelError message={tasks.error.message} />;
  }
  return (
    <TaskTable
      tasks={tasks.isPending ? undefined : tasks.data?.tasks}
      showApp={false}
      showWorkload={false}
      taskLink={(taskId) => ({
        to: "/w/$workspace/apps/$app/workloads/$kind/$name/tasks/$taskId",
        params: {
          workspace,
          app: workload.app,
          kind: workload.kind,
          name: workload.name,
          taskId,
        },
      })}
      emptyMessage="No tasks yet"
      compact
      className="min-h-0 flex-1"
    />
  );
}

function WorkloadSkeleton() {
  return (
    <WorkspacePage
      pending
      title={<Skeleton className="h-7 w-64" />}
      headerDetails={<Skeleton className="h-12 w-full" />}
      contentClassName="grid gap-3 overflow-y-auto xl:grid-cols-[minmax(20rem,2fr)_minmax(0,3fr)] xl:overflow-hidden"
    >
      <Skeleton className="h-[30rem] w-full lg:h-auto lg:min-h-0 lg:flex-1" aria-hidden="true" />
      <Skeleton className="h-[30rem] w-full lg:h-auto lg:min-h-0" aria-hidden="true" />
    </WorkspacePage>
  );
}
