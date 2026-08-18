import { useState } from "react";
import { createFileRoute, Link, Outlet } from "@tanstack/react-router";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";

import { PanelErrorBoundary, RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { Panel } from "@/components/shared/Panel";
import { StatusChip } from "@/components/shared/StatusChip";
import { StubKindIcon } from "@/components/shared/StubKindIcon";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { Skeleton } from "@/components/ui/skeleton";
import { appQueryOptions } from "@/lib/queries/apps";
import { containersQueryOptions, selectContainerList } from "@/lib/queries/containers";
import { deploymentsInfiniteQueryOptions, selectDeploymentList } from "@/lib/queries/deployments";
import { stubsQueryOptions } from "@/lib/queries/stubs";
import { useWorkspace } from "@/lib/workspace-context";

import { currentDeployment, findWorkloadGroup, type WorkloadGroup } from "./-workloads/grouping";
import { LatencyPanel } from "./-workloads/LatencyPanel";
import { Playground } from "./-workloads/Playground";
import { PLAYGROUND_KINDS } from "./-workloads/playground-form";
import {
  ACTIVE_POD_CONTAINER_STATUSES,
  PodInstances,
  type PodInstanceStatusFilter,
} from "./-workloads/PodInstances";
import { VersionHistory } from "./-workloads/VersionHistory";
import { WorkloadActivity } from "./-workloads/WorkloadActivity";
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
      statuses:
        group?.kind === "pod" && podInstanceStatus === "active"
          ? [...ACTIVE_POD_CONTAINER_STATUSES]
          : undefined,
      enabled: Boolean(group),
    }),
  );

  if (deployments.isPending || stubs.isPending || app.isPending) return <WorkloadSkeleton />;
  const loadError = deployments.error ?? stubs.error ?? app.error;
  if (loadError) {
    return <div className="p-4 text-sm text-destructive">{loadError.message}</div>;
  }
  if (!group) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        No deployed workload named {name} in this app
      </div>
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
  const showsInvoke = group.active && PLAYGROUND_KINDS.has(group.kind);
  const showsPerformance = OBSERVABLE_KINDS.has(group.kind);
  const showsControls = showsInvoke || showsPerformance;

  return (
    <WorkspacePage
      title={<span className="mono">{group.name}</span>}
      description={
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="flex items-center gap-1.5">
            <StubKindIcon kind={group.kind} className="size-3.5" />
            {kindLabel(group)}
          </span>
          <span aria-hidden="true">·</span>
          <span className="mono">v{current.version}</span>
          <span aria-hidden="true">·</span>
          <span>{runningContainers.length} running</span>
        </span>
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
      contentClassName="mx-auto flex w-full max-w-[1600px] flex-col gap-3 overflow-y-auto lg:grid lg:overflow-hidden"
      contentStyle={{
        gridTemplateRows: showsControls
          ? "max-content minmax(0, 1.25fr) minmax(0, 0.75fr)"
          : "max-content minmax(0, 1fr)",
      }}
    >
      <section
        aria-label="Workload configuration and versions"
        className="grid min-h-0 shrink-0 gap-3 lg:grid-cols-3"
      >
        <Panel
          title={operationTitle(group)}
          description="Current deployment configuration and capacity"
          contentClassName="overflow-auto p-0 lg:overflow-visible"
          className="min-h-[18rem] lg:col-span-2 lg:min-h-0"
        >
          <WorkloadOperation
            workspaceId={workspace.id}
            deployment={current}
            group={group}
            isPublic={isPublic}
            runningContainers={runningContainers.length}
          />
        </Panel>
        <div className="relative min-h-[18rem] lg:min-h-0">
          <VersionsPanel
            workspaceId={workspace.id}
            group={group}
            appId={appId}
            workspaceName={workspace.name}
            nextCursor={deploymentList.nextCursor}
            loadingMore={deployments.isFetchingNextPage}
            loadMoreError={deployments.isFetchNextPageError}
            onLoadMore={() => void deployments.fetchNextPage()}
          />
        </div>
      </section>

      {showsControls ? (
        <section
          aria-label="Workload controls and performance"
          className={`grid min-h-0 shrink-0 gap-3 ${controlGridClass(showsInvoke, showsPerformance)}`}
        >
          {showsInvoke ? (
            <Panel
              title="Invoke"
              description="Send a request with the deployed contract"
              contentClassName="overflow-hidden p-0"
              className="h-[18rem] min-h-0 lg:h-full"
            >
              <Playground
                workspaceId={workspace.id}
                workspaceName={workspace.name}
                appId={appId}
                workloadName={group.name}
                deploymentId={current.id}
              />
            </Panel>
          ) : null}

          {showsPerformance ? (
            <Panel
              title="Performance"
              description="Latency and outcomes over the last 24 hours"
              contentClassName="overflow-hidden p-3"
              className="min-h-[18rem] lg:h-full lg:min-h-0"
            >
              <PanelErrorBoundary title="Performance could not be displayed">
                <LatencyPanel
                  workspaceId={workspace.id}
                  stubIds={group.stubIds}
                  kind={group.kind}
                />
              </PanelErrorBoundary>
            </Panel>
          ) : null}
        </section>
      ) : null}

      {group.kind === "pod" ? (
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
      ) : (
        <WorkloadActivity
          workspaceId={workspace.id}
          workspaceName={workspace.name}
          appId={appId}
          workloadName={group.name}
          stubIds={group.stubIds}
        />
      )}
    </WorkspacePage>
  );
}

function VersionsPanel({
  workspaceId,
  group,
  appId,
  workspaceName,
  nextCursor,
  loadingMore,
  loadMoreError,
  onLoadMore,
}: {
  workspaceId: string;
  group: WorkloadGroup;
  appId: string;
  workspaceName: string;
  nextCursor: string | undefined;
  loadingMore: boolean;
  loadMoreError: boolean;
  onLoadMore: () => void;
}) {
  return (
    <Panel
      title="Versions"
      description="Deployment history and controls"
      contentClassName="overflow-auto p-0"
      className="min-h-[18rem] lg:absolute lg:inset-0 lg:min-h-0"
    >
      <VersionHistory
        group={group}
        appId={appId}
        workspaceId={workspaceId}
        workspaceName={workspaceName}
        nextCursor={nextCursor}
        loadingMore={loadingMore}
        loadMoreError={loadMoreError}
        onLoadMore={onLoadMore}
      />
    </Panel>
  );
}

function WorkloadSkeleton() {
  return (
    <WorkspacePage
      title={<Skeleton className="h-7 w-64" />}
      contentClassName="mx-auto flex w-full max-w-[1600px] flex-col gap-3 overflow-y-auto lg:grid lg:overflow-hidden"
      contentStyle={{
        gridTemplateRows: "max-content minmax(0, 1.25fr) minmax(0, 0.75fr)",
      }}
    >
      <div className="grid min-h-0 gap-3 lg:h-48 lg:grid-cols-3" aria-hidden="true">
        <Skeleton className="h-72 w-full lg:col-span-2 lg:h-full" />
        <Skeleton className="h-72 w-full lg:h-full" />
      </div>
      <div className="grid min-h-0 gap-3 lg:grid-cols-2" aria-hidden="true">
        <Skeleton className="h-72 w-full lg:h-full" />
        <Skeleton className="h-72 w-full lg:h-full" />
      </div>
      <Skeleton className="h-80 w-full lg:h-full" aria-hidden="true" />
    </WorkspacePage>
  );
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
  if (group.kind === "pod") return "Pod configuration";
  if (group.kind === "endpoint" || group.kind === "asgi") return "HTTP configuration";
  return "Function configuration";
}

function controlGridClass(showsInvoke: boolean, showsPerformance: boolean): string {
  return showsInvoke && showsPerformance ? "lg:grid-cols-2" : "lg:grid-cols-1";
}
