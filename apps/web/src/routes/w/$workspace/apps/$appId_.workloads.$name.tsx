import { useState } from "react";
import { createFileRoute, Link, Outlet } from "@tanstack/react-router";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";

import { PanelErrorBoundary, RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { LinearTab, LinearTabsList } from "@/components/shared/LinearSelect";
import { Panel } from "@/components/shared/Panel";
import { PanelError } from "@/components/shared/PanelError";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { StatusChip } from "@/components/shared/StatusChip";
import { StubKindIcon } from "@/components/shared/StubKindIcon";
import { TaskTable } from "@/components/shared/TaskTable";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { countLabel } from "@/components/shared/WorkspacePage/countLabel";
import { PageFacts } from "@/components/shared/WorkspacePage/PageFacts";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { appQueryOptions } from "@/lib/queries/apps";
import { containersQueryOptions, selectContainerList } from "@/lib/queries/containers";
import { deploymentsInfiniteQueryOptions, selectDeploymentList } from "@/lib/queries/deployments";
import { stubsQueryOptions, taskLatencyQueryOptions } from "@/lib/queries/stubs";
import { tasksQueryOptions } from "@/lib/queries/tasks";
import { useWorkspace } from "@/lib/workspace-context";

import { currentDeployment, findWorkloadGroup, type WorkloadGroup } from "./-workloads/grouping";
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
  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <WorkloadDetailPage />
      <Outlet />
    </div>
  );
}

/**
 * One workload, in two regions: where it answers from, and one inspector
 * holding everything a reader looks at one at a time. Five panels competing for
 * the same viewport meant the chart, the versions and the task table were each
 * given a third of the room they needed and shown whether or not anyone was
 * reading them.
 */
function WorkloadDetailPage() {
  const { appId, name } = Route.useParams();
  const { workspace } = useWorkspace();
  const [podInstanceStatus, setPodInstanceStatus] = useState<PodInstanceStatusFilter>("active");
  const app = useQuery(appQueryOptions(workspace.id, appId));
  const deployments = useInfiniteQuery(
    deploymentsInfiniteQueryOptions(workspace.id, { appId, name }),
  );
  const stubs = useQuery(stubsQueryOptions(workspace.id, appId));

  const deploymentList = selectDeploymentList(deployments.data, deployments.hasNextPage);
  const group = findWorkloadGroup(deploymentList.items, appId, name);
  const selectedDeployment = group ? currentDeployment(group) : undefined;
  const containerStubIds =
    group?.kind === "pod" && selectedDeployment?.stub_id
      ? [selectedDeployment.stub_id]
      : group?.stubIds;
  const containers = useInfiniteQuery(
    containersQueryOptions(workspace.id, {
      appId,
      stubIds: containerStubIds,
      // Only a Pod lists its containers; every other kind reports how many are
      // running, and asking the server for those is what makes the count right
      // rather than right about the newest page of a long history.
      statuses: podContainerStatuses(group?.kind, podInstanceStatus),
      enabled: Boolean(group),
    }),
  );
  const latency = useQuery({
    ...taskLatencyQueryOptions(workspace.id, group?.stubIds ?? []),
    enabled: Boolean(group) && OBSERVABLE_KINDS.has(group?.kind ?? ""),
  });

  if (deployments.isPending || stubs.isPending || app.isPending) return <WorkloadSkeleton />;
  const loadError = deployments.error ?? stubs.error ?? app.error;
  if (loadError) {
    return <PanelError message={loadError.message} />;
  }
  if (!group) {
    return (
      <PanelEmpty message={`No deployed workload named ${name} in this app`} className="h-full" />
    );
  }

  const current = currentDeployment(group);
  const currentStub = stubs.data?.stubs.find((stub) => stub.id === current.stub_id);
  const containerList = selectContainerList(containers.data, containers.hasNextPage);
  const workloadContainers = containerList.items
    .map((item) => item.container)
    .sort((left, right) => right.created_at.localeCompare(left.created_at));
  const runningContainers = workloadContainers.filter(
    (container) => container.status === "running",
  );
  const isPublic = Boolean(app.data?.public || currentStub?.public);
  const isPod = group.kind === "pod";
  const showsInvoke = group.active && PLAYGROUND_KINDS.has(group.kind);
  // The chart earns its band only where there is something in the window to
  // plot; otherwise the task table below states the same silence once.
  const showsLatency =
    OBSERVABLE_KINDS.has(group.kind) &&
    (latency.isPending || latency.isError || latencyHasSignal(latency.data?.buckets));

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
            countLabel(runningContainers.length, "running", "running"),
          ]}
        />
      }
      actions={
        <>
          <StatusChip status={group.active ? "deployed" : "stopped"} live={group.active} />
          <Link
            to="/w/$workspace/apps/$appId"
            params={{ workspace: workspace.name, appId }}
            className="flex h-8 min-w-0 items-center gap-1.5 rounded-md border border-input bg-card px-2.5 text-xs text-muted-foreground outline-none transition-colors hover:border-brand/40 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ArrowLeft className="size-3.5 shrink-0" aria-hidden="true" />
            {app.data?.name ?? "View app"}
          </Link>
        </>
      }
      contentClassName="flex flex-col gap-3 overflow-y-auto lg:overflow-hidden"
    >
      <Panel
        title={operationTitle(group)}
        contentClassName="overflow-hidden p-0"
        className="shrink-0"
      >
        <WorkloadOperation workspaceId={workspace.id} deployment={current} group={group} />
      </Panel>

      <Tabs
        key={`${appId}:${group.name}`}
        defaultValue={defaultInspectorTab(group, showsInvoke)}
        /* Below `lg` the region takes the height of whichever view is open and
           the page scrolls once; above it, the region fills the viewport and
           each view scrolls inside itself. */
        className="panel flex flex-col overflow-hidden rounded-md max-lg:shrink-0 lg:min-h-0 lg:flex-1"
      >
        <LinearTabsList
          ariaLabel="Workload inspector views"
          className="min-h-11 shrink-0 bg-card px-2"
        >
          {showsInvoke ? <LinearTab value="invoke">Invoke</LinearTab> : null}
          {isPod ? (
            <LinearTab value="instances">Instances</LinearTab>
          ) : (
            <LinearTab value="activity">Activity</LinearTab>
          )}
          <LinearTab value="versions">Versions</LinearTab>
          <LinearTab value="configuration">Configuration</LinearTab>
        </LinearTabsList>

        {showsInvoke ? (
          <TabsContent value="invoke" className="m-0 min-h-0 flex-1 overflow-hidden">
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
              error={containers.error}
              nextCursor={containerList.nextCursor}
              loadingMore={containers.isFetchingNextPage}
              loadMoreError={containers.isFetchNextPageError}
              onLoadMore={() => void containers.fetchNextPage()}
            />
          </TabsContent>
        ) : (
          <TabsContent
            value="activity"
            className="m-0 flex min-h-0 flex-1 flex-col overflow-hidden"
          >
            {showsLatency ? (
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
            ) : null}
            <WorkloadRuns
              workspaceId={workspace.id}
              workspaceName={workspace.name}
              appId={appId}
              workloadName={group.name}
              stubIds={group.stubIds}
            />
          </TabsContent>
        )}

        <TabsContent value="versions" className="m-0 min-h-0 flex-1 overflow-auto">
          <VersionHistory
            group={group}
            appId={appId}
            workspaceId={workspace.id}
            workspaceName={workspace.name}
            nextCursor={deploymentList.nextCursor}
            loadingMore={deployments.isFetchingNextPage}
            loadMoreError={deployments.isFetchNextPageError}
            onLoadMore={() => void deployments.fetchNextPage()}
          />
        </TabsContent>

        <TabsContent value="configuration" className="m-0 min-h-0 flex-1 overflow-auto">
          <WorkloadConfiguration deployment={current} kind={group.kind} />
        </TabsContent>
      </Tabs>
    </WorkspacePage>
  );
}

function WorkloadRuns({
  workspaceId,
  workspaceName,
  appId,
  workloadName,
  stubIds,
}: {
  workspaceId: string;
  workspaceName: string;
  appId: string;
  workloadName: string;
  stubIds: string[];
}) {
  const tasks = useQuery(tasksQueryOptions(workspaceId, { limit: 50, appId, stubIds }));
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
      emptyMessage="No tasks recorded yet"
      className="min-h-0 flex-1"
    />
  );
}

function WorkloadSkeleton() {
  return (
    <WorkspacePage
      title={<Skeleton className="h-7 w-64" />}
      contentClassName="flex flex-col gap-3 overflow-y-auto lg:overflow-hidden"
    >
      <Skeleton className="h-28 w-full shrink-0" aria-hidden="true" />
      <Skeleton className="h-[30rem] w-full lg:h-auto lg:min-h-0 lg:flex-1" aria-hidden="true" />
    </WorkspacePage>
  );
}

/**
 * The view a reader lands on is whatever the workload does without being asked:
 * a schedule and a Pod are already working, so their record is the answer,
 * while a function or endpoint waits to be called.
 */
function defaultInspectorTab(group: WorkloadGroup, showsInvoke: boolean): string {
  if (group.kind === "pod") return "instances";
  if (showsInvoke && !group.latest.spec?.cron) return "invoke";
  return "activity";
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

function operationTitle(group: WorkloadGroup): string {
  if (group.latest.spec?.cron) return "Schedule";
  if (group.kind === "pod") return "Pod";
  if (group.kind === "endpoint" || group.kind === "asgi") return "HTTP endpoint";
  return "Function";
}
