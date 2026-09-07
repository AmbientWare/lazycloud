import { useState } from "react";
import { createFileRoute, Link, Outlet } from "@tanstack/react-router";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";

import { PanelErrorBoundary, RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { LinearTab, LinearTabsList } from "@/components/shared/LinearSelect";
import { Panel } from "@/components/shared/Panel";
import { PanelError } from "@/components/shared/PanelError";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { StubKindIcon } from "@/components/shared/StubKindIcon";
import { TaskTable } from "@/components/shared/TaskTable";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { PageFacts } from "@/components/shared/WorkspacePage/PageFacts";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { countLabel } from "@/lib/format";
import { appQueryOptions } from "@/lib/queries/apps";
import { containersQueryOptions, selectContainerList } from "@/lib/queries/containers";
import {
  deploymentsInfiniteQueryOptions,
  selectDeploymentList,
  workloadQueryOptions,
} from "@/lib/queries/deployments";
import { taskLatencyQueryOptions } from "@/lib/queries/stubs";
import { tasksQueryOptions } from "@/lib/queries/tasks";
import { useWorkspace } from "@/lib/workspace-context";

import { type WorkloadGroup } from "./-workloads/grouping";
import { CallMethods } from "./-workloads/CallMethods";
import { LatencyPanel, latencyHasSignal } from "./-workloads/LatencyPanel";
import { Playground } from "./-workloads/Playground";
import { PLAYGROUND_KINDS } from "./-workloads/playground-form";
import {
  ACTIVE_POD_CONTAINER_STATUSES,
  PodInstances,
  type PodInstanceStatusFilter,
} from "./-workloads/PodInstances";
import { VersionHistory } from "./-workloads/VersionHistory";
import { WorkloadConfiguration } from "./-workloads/WorkloadConfiguration";
import { WorkloadOperation } from "./-workloads/WorkloadOperation";

export const Route = createFileRoute("/w/$workspace/apps/$appId_/workloads/$name")({
  component: WorkloadDetailRoute,
  errorComponent: RouteErrorFallback,
});

const OBSERVABLE_KINDS = new Set(["function", "endpoint", "asgi"]);

function WorkloadDetailRoute() {
  const { appId, name } = Route.useParams();
  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <WorkloadDetailPage key={`${appId}:${name}`} />
      <Outlet />
    </div>
  );
}

function WorkloadDetailPage() {
  const { appId, name } = Route.useParams();
  const { workspace } = useWorkspace();
  const [podInstanceStatus, setPodInstanceStatus] = useState<PodInstanceStatusFilter>("active");
  const app = useQuery(appQueryOptions(workspace.id, appId));
  const deployments = useInfiniteQuery(
    deploymentsInfiniteQueryOptions(workspace.id, { appId, name }),
  );
  const workload = useQuery(workloadQueryOptions(workspace.id, appId, name));
  const [inspectorTab, setInspectorTab] = useState<string | null>(null);

  const deploymentList = selectDeploymentList(deployments.data, deployments.hasNextPage);
  const selectedDeployment = workload.data?.deployment;
  const group: WorkloadGroup | undefined = selectedDeployment
    ? {
        name: selectedDeployment.name,
        kind: selectedDeployment.kind,
        active: selectedDeployment.active,
        latest: selectedDeployment,
        deployments: deploymentList.items,
        stubIds: selectedDeployment.stub_id ? [selectedDeployment.stub_id] : [],
      }
    : undefined;
  const containerStubIds =
    group?.kind === "pod" && selectedDeployment?.stub_id
      ? [selectedDeployment.stub_id]
      : group?.stubIds;
  const containers = useInfiniteQuery(
    containersQueryOptions(workspace.id, {
      appId,
      stubIds: containerStubIds,
      statuses: podContainerStatuses(group?.kind, podInstanceStatus),
      enabled: group?.kind === "pod",
    }),
  );
  if (workload.isPending || app.isPending) return <WorkloadSkeleton />;
  const loadError = !workload.data ? workload.error : !app.data ? app.error : null;
  if (loadError) {
    return <PanelError message={loadError.message} />;
  }
  if (!group) {
    return (
      <PanelEmpty message={`No deployed workload named ${name} in this app`} className="h-full" />
    );
  }

  const current = group.latest;
  const containerList = selectContainerList(containers.data, containers.hasNextPage);
  const workloadContainers = containerList.items
    .map((item) => item.container)
    .sort((left, right) => right.created_at.localeCompare(left.created_at));
  const isPublic = workload.data?.public;
  const isPod = group.kind === "pod";
  const supportsInvoke = PLAYGROUND_KINDS.has(group.kind);
  const showsInvoke = group.active && PLAYGROUND_KINDS.has(group.kind);

  return (
    <WorkspacePage
      title={<span className="mono">{group.name}</span>}
      description={
        <PageFacts
          items={[
            <span key="kind" className="flex items-center gap-1.5">
              <StubKindIcon kind={group.kind} className="size-3.5" />
              {kindLabel(group)}
            </span>,
            <span key="version" className="mono">
              v{current.version}
            </span>,
            isPublic ? "Public" : "Token required",
            workload.data
              ? countLabel(workload.data.running_containers, "running", "running")
              : null,
          ]}
        />
      }
      actions={
        <Link
          to="/w/$workspace/apps/$appId"
          params={{ workspace: workspace.name, appId }}
          className="flex h-8 min-w-0 items-center gap-1.5 rounded-md border border-input bg-card px-2.5 text-xs text-muted-foreground outline-none transition-colors hover:border-brand/40 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        >
          <ArrowLeft className="size-3.5 shrink-0" aria-hidden="true" />
          {app.data?.name ?? "View app"}
        </Link>
      }
      headerDetails={
        <WorkloadOperation workspaceId={workspace.id} deployment={current} group={group} />
      }
      contentClassName={
        isPod
          ? "flex flex-col overflow-y-auto lg:overflow-hidden"
          : "grid gap-3 overflow-y-auto xl:grid-cols-[minmax(20rem,2fr)_minmax(0,3fr)] xl:overflow-hidden"
      }
    >
      <Tabs
        key={`${appId}:${group.name}`}
        value={
          inspectorTab && (inspectorTab !== "invoke" || showsInvoke)
            ? inspectorTab
            : defaultInspectorTab(group, showsInvoke)
        }
        onValueChange={setInspectorTab}
        className="panel flex flex-col overflow-hidden rounded-md max-lg:shrink-0 lg:min-h-0 lg:flex-1"
      >
        <LinearTabsList
          ariaLabel="Workload inspector views"
          className="min-h-11 shrink-0 bg-card px-2"
        >
          {showsInvoke ? <LinearTab value="invoke">Invoke</LinearTab> : null}
          {isPod ? <LinearTab value="instances">Instances</LinearTab> : null}
          {supportsInvoke ? <LinearTab value="call">Call</LinearTab> : null}
          <LinearTab value="versions">Versions</LinearTab>
          <LinearTab value="configuration">Configuration</LinearTab>
        </LinearTabsList>

        {showsInvoke ? (
          <TabsContent value="invoke" className="m-0 min-h-0 flex-1 overflow-auto">
            <PanelErrorBoundary title="Invoke could not be displayed">
              <Playground
                workspaceId={workspace.id}
                workspaceName={workspace.name}
                appId={appId}
                workloadName={group.name}
                deploymentId={current.id}
              />
            </PanelErrorBoundary>
          </TabsContent>
        ) : null}

        {supportsInvoke ? (
          <TabsContent value="call" className="m-0 min-h-0 flex-1 overflow-auto">
            <PanelErrorBoundary title="Call methods could not be displayed">
              <CallMethods workspaceId={workspace.id} deploymentId={current.id} />
            </PanelErrorBoundary>
          </TabsContent>
        ) : null}

        {isPod ? (
          <TabsContent value="instances" className="m-0 min-h-0 flex-1 overflow-hidden">
            <PodInstances
              workspaceId={workspace.id}
              workspaceName={workspace.name}
              appId={appId}
              workloadName={group.name}
              deployment={current}
              statusFilter={podInstanceStatus}
              onStatusFilterChange={setPodInstanceStatus}
              containers={workloadContainers}
              loading={containers.isPending}
              error={containers.isError && !containers.data ? containers.error : null}
              nextCursor={containerList.nextCursor}
              loadingMore={containers.isFetchingNextPage}
              loadMoreError={containers.isFetchNextPageError}
              onLoadMore={() => void containers.fetchNextPage()}
            />
          </TabsContent>
        ) : null}

        <TabsContent value="versions" className="m-0 min-h-0 flex-1 overflow-auto">
          {deployments.isPending ? (
            <Skeleton className="m-4 h-36" />
          ) : deployments.isError && !deployments.data ? (
            <PanelError message={deployments.error.message} />
          ) : (
            <VersionHistory
              group={group}
              versionCount={workload.data?.version_count ?? 0}
              appId={appId}
              workspaceId={workspace.id}
              workspaceName={workspace.name}
              nextCursor={deploymentList.nextCursor}
              loadingMore={deployments.isFetchingNextPage}
              loadMoreError={deployments.isFetchNextPageError}
              onLoadMore={() => void deployments.fetchNextPage()}
            />
          )}
        </TabsContent>

        <TabsContent value="configuration" className="m-0 min-h-0 flex-1 overflow-auto">
          <WorkloadConfiguration deployment={current} kind={group.kind} />
        </TabsContent>
      </Tabs>

      {!isPod ? (
        <Panel
          title="Activity"
          description="Recent tasks and 24-hour latency"
          contentClassName="flex flex-col overflow-hidden p-0"
          className="min-h-[24rem] lg:min-h-0"
        >
          <WorkloadLatency workspaceId={workspace.id} appId={appId} group={group} />
          <WorkloadRuns
            workspaceId={workspace.id}
            workspaceName={workspace.name}
            appId={appId}
            workloadName={group.name}
          />
        </Panel>
      ) : null}
    </WorkspacePage>
  );
}

function WorkloadLatency({
  workspaceId,
  appId,
  group,
}: {
  workspaceId: string;
  appId: string;
  group: WorkloadGroup;
}) {
  const observable = OBSERVABLE_KINDS.has(group.kind);
  const latency = useQuery({
    ...taskLatencyQueryOptions(workspaceId, appId, group.name),
    enabled: observable,
  });

  if (
    !observable ||
    !(latency.isPending || latency.isError || latencyHasSignal(latency.data?.buckets))
  ) {
    return null;
  }

  return (
    <div className="h-52 shrink-0 border-b border-border/80 p-3">
      <PanelErrorBoundary title="Performance could not be displayed">
        <LatencyPanel
          buckets={latency.data?.buckets}
          pending={latency.isPending}
          error={latency.error}
          kind={group.kind}
        />
      </PanelErrorBoundary>
    </div>
  );
}

function WorkloadRuns({
  workspaceId,
  workspaceName,
  appId,
  workloadName,
}: {
  workspaceId: string;
  workspaceName: string;
  appId: string;
  workloadName: string;
}) {
  const tasks = useQuery(tasksQueryOptions(workspaceId, { limit: 50, appId, workloadName }));
  if (tasks.isError) {
    return <PanelError message={tasks.error.message} />;
  }
  return (
    <TaskTable
      tasks={tasks.isPending ? undefined : tasks.data?.data}
      showApp={false}
      showWorkload={false}
      taskLink={(taskId) => ({
        to: "/w/$workspace/apps/$appId/workloads/$name/tasks/$taskId",
        params: { workspace: workspaceName, appId, name: workloadName, taskId },
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
      title={<Skeleton className="h-7 w-64" />}
      headerDetails={<Skeleton className="h-12 w-full" />}
      contentClassName="grid gap-3 overflow-y-auto xl:grid-cols-[minmax(20rem,2fr)_minmax(0,3fr)] xl:overflow-hidden"
    >
      <Skeleton className="h-[30rem] w-full lg:h-auto lg:min-h-0 lg:flex-1" aria-hidden="true" />
      <Skeleton className="h-[30rem] w-full lg:h-auto lg:min-h-0" aria-hidden="true" />
    </WorkspacePage>
  );
}

function defaultInspectorTab(group: WorkloadGroup, showsInvoke: boolean): string {
  if (group.kind === "pod") return "instances";
  if (showsInvoke && !group.latest.spec?.cron) return "invoke";
  return "versions";
}

function podContainerStatuses(
  kind: string | undefined,
  filter: PodInstanceStatusFilter,
): ("pending" | "running")[] | undefined {
  if (kind !== "pod") return ["running"];
  return filter === "active" ? [...ACTIVE_POD_CONTAINER_STATUSES] : undefined;
}

function kindLabel(group: WorkloadGroup): string {
  // A scheduled function still reads as a schedule here: it is what the person
  // looking at the list is scanning for, even though it is a function.
  if (group.latest.spec?.cron) return "Schedule";
  const labels: Record<string, string> = {
    function: "Function",
    endpoint: "Endpoint",
    asgi: "ASGI",
    pod: "Pod",
  };
  return labels[group.kind] ?? "Workload";
}
