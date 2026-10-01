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
import { deploymentsInfiniteQueryOptions, selectDeploymentList } from "@/lib/queries/deployments";
import { deployedStubsQueryOptions, taskLatencyQueryOptions } from "@/lib/queries/stubs";
import { requestsQueryOptions, servesRequests, tasksQueryOptions } from "@/lib/queries/tasks";
import { useWorkspace } from "@/lib/workspace-context";

import { currentDeployment, findWorkloadGroup, type WorkloadGroup } from "./-workloads/grouping";
import { CallMethods } from "./-workloads/CallMethods";
import { DevboxActions, DevboxConnect, DevboxWorkspace } from "./-workloads/DevboxDetail";
import { LatencyPanel, latencyHasSignal } from "./-workloads/LatencyPanel";
import { Playground } from "./-workloads/Playground";
import { PLAYGROUND_KINDS } from "./-workloads/playground-form";
import { PodInstances, type PodInstanceStatusFilter } from "./-workloads/PodInstances";
import { VersionHistory } from "./-workloads/VersionHistory";
import { WorkloadConfiguration } from "./-workloads/WorkloadConfiguration";
import { WorkloadOperation } from "./-workloads/WorkloadOperation";

export const Route = createFileRoute("/w/$workspace/apps/$app_/workloads/$kind/$name")({
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
  const { app, kind, name } = Route.useParams();
  const { workspace } = useWorkspace();
  const [podInstanceStatus, setPodInstanceStatus] = useState<PodInstanceStatusFilter>("active");
  const appRecord = useQuery(appQueryOptions(workspace.id, app));
  const deployments = useInfiniteQuery(
    deploymentsInfiniteQueryOptions(workspace.id, { appId: app, kind, name }),
  );
  const stubs = useQuery(deployedStubsQueryOptions(workspace.id, app));

  const deploymentList = selectDeploymentList(deployments.data, deployments.hasNextPage);
  const group = findWorkloadGroup(deploymentList.items, app, kind, name);
  const selectedDeployment = group ? currentDeployment(group) : undefined;
  const isDevbox = selectedDeployment?.role === "devbox";
  const containers = useInfiniteQuery(
    containersQueryOptions(workspace.name, {
      app,
      function: name,
      // Only a Pod lists its containers; every other kind reports how many are
      // running, and asking the server for those is what makes the count right
      // rather than right about the newest page of a long history.
      live: group?.kind !== "pod" || podInstanceStatus === "active",
      // A devbox is one machine; its status endpoint names the container.
      enabled: Boolean(group) && !isDevbox,
    }),
  );
  if (deployments.isPending || stubs.isPending || appRecord.isPending) return <WorkloadSkeleton />;
  const loadError =
    (deployments.isFetchNextPageError ? null : deployments.error) ?? stubs.error ?? appRecord.error;
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
  const runningContainers = containerList.items.filter((container) => container.state === "ready");
  const isPublic = Boolean(appRecord.data?.public || currentStub?.public);
  const isPod = group.kind === "pod";
  const kindFact = (
    <span key="kind" className="flex items-center gap-1.5">
      <StubKindIcon kind={group.kind} className="size-3.5" />
      {kindLabel(group)}
    </span>
  );
  const backLink = (
    <Link
      to="/w/$workspace/apps/$app"
      params={{ workspace: workspace.name, app }}
      className="flex h-8 min-w-0 items-center gap-1.5 rounded-md border border-input bg-card px-2.5 text-xs text-muted-foreground outline-none transition-colors hover:border-brand/40 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
    >
      <ArrowLeft className="size-3.5 shrink-0" aria-hidden="true" />
      {appRecord.data?.name ?? "View app"}
    </Link>
  );

  if (current.role === "devbox") {
    return (
      <WorkspacePage
        title={<span className="mono">{group.name}</span>}
        description={
          <PageFacts
            items={[
              kindFact,
              <span key="version" className="mono">
                v{current.version}
              </span>,
              isPublic ? "Public" : "Token required",
            ]}
          />
        }
        actions={
          <>
            <DevboxActions workspaceId={workspace.id} deploymentId={current.id} />
            {backLink}
          </>
        }
        headerDetails={
          <PanelErrorBoundary title="Devbox status could not be displayed">
            <DevboxConnect workspaceId={workspace.id} deploymentId={current.id} />
          </PanelErrorBoundary>
        }
        contentClassName="flex flex-col gap-3 overflow-y-auto xl:grid xl:grid-cols-[minmax(0,3fr)_minmax(22rem,2fr)] xl:overflow-hidden"
      >
        <DevboxWorkspace
          workspaceId={workspace.id}
          workspaceName={workspace.name}
          app={app}
          group={group}
          deploymentId={current.id}
          nextCursor={deploymentList.nextCursor}
          loadingMore={deployments.isFetchingNextPage}
          loadMoreError={deployments.isFetchNextPageError}
          onLoadMore={() => void deployments.fetchNextPage()}
        />
      </WorkspacePage>
    );
  }

  const showsInvoke = group.active && PLAYGROUND_KINDS.has(group.kind);

  return (
    <WorkspacePage
      title={<span className="mono">{group.name}</span>}
      description={
        <PageFacts
          items={[
            kindFact,
            <span key="version" className="mono">
              v{current.version}
            </span>,
            isPublic ? "Public" : "Token required",
            containers.data ? countLabel(runningContainers.length, "running", "running") : null,
          ]}
        />
      }
      actions={backLink}
      headerDetails={
        <WorkloadOperation workspaceId={workspace.id} deployment={current} group={group} />
      }
      contentClassName={
        isPod
          ? "flex flex-col overflow-y-auto lg:overflow-hidden"
          : "flex flex-col gap-3 overflow-y-auto xl:grid xl:grid-cols-[minmax(20rem,2fr)_minmax(0,3fr)] xl:overflow-hidden"
      }
    >
      <Tabs
        key={JSON.stringify([app, group.kind, group.name])}
        defaultValue={defaultInspectorTab(group, showsInvoke)}
        className={`panel flex flex-col overflow-hidden rounded-md ${isPod ? "min-h-0 flex-1" : "shrink-0 xl:min-h-0"}`}
      >
        <LinearTabsList
          ariaLabel="Workload inspector views"
          className="min-h-11 shrink-0 bg-card px-2"
        >
          {showsInvoke ? <LinearTab value="invoke">Invoke</LinearTab> : null}
          {isPod ? <LinearTab value="instances">Instances</LinearTab> : null}
          <LinearTab value="versions">Versions</LinearTab>
          <LinearTab value="configuration">Configuration</LinearTab>
          {OBSERVABLE_KINDS.has(group.kind) ? <LinearTab value="call">Call</LinearTab> : null}
        </LinearTabsList>

        {showsInvoke ? (
          <TabsContent value="invoke" className="m-0 min-h-0 flex-1 overflow-hidden">
            <PanelErrorBoundary title="Invoke could not be displayed">
              <Playground
                workspaceId={workspace.id}
                workspaceName={workspace.name}
                app={app}
                workloadName={group.name}
                workloadKind={group.kind}
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
              app={app}
              workloadName={group.name}
              deployment={current}
              statusFilter={podInstanceStatus}
              onStatusFilterChange={setPodInstanceStatus}
              // Pods have no API until the workloads packet.
              containers={[]}
              loading={containers.isPending}
              error={containers.isFetchNextPageError ? null : containers.error}
              nextCursor={containerList.nextCursor}
              loadingMore={containers.isFetchingNextPage}
              loadMoreError={containers.isFetchNextPageError}
              onLoadMore={() => void containers.fetchNextPage()}
            />
          </TabsContent>
        ) : null}

        <TabsContent
          value="versions"
          className={`m-0 min-h-0 flex-1 overflow-auto ${isPod ? "" : "max-xl:flex-none"}`}
        >
          <VersionHistory
            group={group}
            app={app}
            workspaceId={workspace.id}
            workspaceName={workspace.name}
            nextCursor={deploymentList.nextCursor}
            loadingMore={deployments.isFetchingNextPage}
            loadMoreError={deployments.isFetchNextPageError}
            onLoadMore={() => void deployments.fetchNextPage()}
          />
        </TabsContent>

        <TabsContent
          value="configuration"
          className={`m-0 min-h-0 flex-1 overflow-auto ${isPod ? "" : "max-xl:flex-none"}`}
        >
          <WorkloadConfiguration deployment={current} kind={group.kind} />
        </TabsContent>
        {OBSERVABLE_KINDS.has(group.kind) && (
          <TabsContent value="call" className="m-0 min-h-0 flex-1 overflow-auto max-xl:flex-none">
            <PanelErrorBoundary title="Call methods could not be displayed">
              <CallMethods
                workspaceId={workspace.id}
                workspaceName={workspace.name}
                deploymentId={current.id}
                handler={currentStub?.handler}
              />
            </PanelErrorBoundary>
          </TabsContent>
        )}
      </Tabs>

      {!isPod ? (
        <Panel
          title="Activity"
          contentClassName="flex flex-col overflow-hidden p-0"
          className="min-h-[24rem] shrink-0 xl:min-h-0"
        >
          <WorkloadLatency workspaceId={workspace.id} group={group} />
          <WorkloadRuns
            workspaceName={workspace.name}
            app={app}
            workloadName={group.name}
            workloadKind={group.kind}
          />
        </Panel>
      ) : null}
    </WorkspacePage>
  );
}

function WorkloadLatency({ workspaceId, group }: { workspaceId: string; group: WorkloadGroup }) {
  const observable = OBSERVABLE_KINDS.has(group.kind);
  const latency = useQuery({
    ...taskLatencyQueryOptions(workspaceId, group.stubIds),
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
  workspaceName,
  app,
  workloadName,
  workloadKind,
}: {
  workspaceName: string;
  app: string;
  workloadName: string;
  workloadKind: string;
}) {
  const requests = servesRequests(workloadKind);
  const tasks = useQuery({
    ...tasksQueryOptions(workspaceName, { app, function: workloadName }),
    enabled: !requests,
  });
  const served = useQuery({
    ...requestsQueryOptions(workspaceName, app, workloadName),
    enabled: requests,
  });
  const rows = requests ? served : tasks;
  if (rows.isError) {
    return <PanelError message={rows.error.message} />;
  }
  return (
    <TaskTable
      tasks={rows.isPending ? undefined : rows.data}
      showApp={false}
      showWorkload={false}
      taskLink={(taskId) => ({
        to: "/w/$workspace/apps/$app/workloads/$kind/$name/tasks/$taskId",
        params: { workspace: workspaceName, app, kind: workloadKind, name: workloadName, taskId },
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

function defaultInspectorTab(group: WorkloadGroup, showsInvoke: boolean): string {
  if (group.kind === "pod") return "instances";
  if (showsInvoke && !group.latest.spec?.cron) return "invoke";
  return "versions";
}

function kindLabel(group: WorkloadGroup): string {
  // A scheduled function still reads as a schedule here: it is what the person
  // looking at the list is scanning for, even though it is a function.
  if (group.latest.spec?.cron) return "Schedule";
  if (group.latest.role === "devbox") return "Devbox";
  const labels: Record<string, string> = {
    function: "Function",
    endpoint: "Endpoint",
    asgi: "ASGI",
    pod: "Pod",
  };
  return labels[group.kind] ?? "Workload";
}
