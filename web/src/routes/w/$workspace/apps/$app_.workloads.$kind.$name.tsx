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
import type { Schemas } from "@/lib/api/client";
import { countLabel } from "@/lib/format";
import { containersQueryOptions, selectContainerList } from "@/lib/queries/containers";
import {
  performanceQueryOptions,
  WorkloadNotFoundError,
  workloadQueryOptions,
  workloadRunning,
  type Workload,
} from "@/lib/queries/deployments";
import { requestsQueryOptions, servesRequests, tasksQueryOptions } from "@/lib/queries/tasks";
import { useWorkspace } from "@/lib/workspace-context";

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

const OBSERVABLE_KINDS = new Set<Schemas["WorkloadKind"]>(["function", "endpoint", "asgi"]);

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
  const query = useQuery(workloadQueryOptions(workspace.name, app, kind, name));
  const loaded = query.data?.deployment;
  // Only a Pod lists its containers; a devbox is one machine whose status names it.
  const listsInstances = loaded?.kind === "pod" && loaded.role !== "devbox";
  const containers = useInfiniteQuery(
    containersQueryOptions(workspace.name, {
      deployment: loaded?.id,
      live: podInstanceStatus === "active",
      enabled: listsInstances,
    }),
  );

  if (query.isPending) return <WorkloadSkeleton />;
  if (query.error instanceof WorkloadNotFoundError) {
    return <PanelEmpty message={query.error.message} className="h-full" />;
  }
  if (query.isError) return <PanelError message={query.error.message} />;

  const workload = query.data;
  const { deployment, release } = workload;
  const isPublic = release.spec.authorized === false;
  const isPod = deployment.kind === "pod";
  const kindFact = (
    <span key="kind" className="flex items-center gap-1.5">
      <StubKindIcon kind={deployment.kind} className="size-3.5" />
      {kindLabel(workload)}
    </span>
  );
  const backLink = (
    <Link
      to="/w/$workspace/apps/$app"
      params={{ workspace: workspace.name, app }}
      className="flex h-8 min-w-0 items-center gap-1.5 rounded-md border border-input bg-card px-2.5 text-xs text-muted-foreground outline-none transition-colors hover:border-brand/40 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
    >
      <ArrowLeft className="size-3.5 shrink-0" aria-hidden="true" />
      {deployment.app}
    </Link>
  );

  if (deployment.role === "devbox") {
    return (
      <WorkspacePage
        title={<span className="mono">{deployment.name}</span>}
        description={
          <PageFacts
            items={[
              kindFact,
              <span key="version" className="mono">
                v{deployment.version}
              </span>,
              isPublic ? "Public" : "Token required",
            ]}
          />
        }
        actions={
          <>
            <DevboxActions workspace={workspace.name} deploymentId={deployment.id} />
            {backLink}
          </>
        }
        headerDetails={
          <PanelErrorBoundary title="Devbox status could not be displayed">
            <DevboxConnect workspace={workspace.name} deploymentId={deployment.id} />
          </PanelErrorBoundary>
        }
        contentClassName="flex flex-col gap-3 overflow-y-auto xl:grid xl:grid-cols-[minmax(0,3fr)_minmax(22rem,2fr)] xl:overflow-hidden"
      >
        <DevboxWorkspace workspace={workspace.name} workload={deployment} spec={release.spec} />
      </WorkspacePage>
    );
  }

  const showsInvoke = workloadRunning(deployment) && PLAYGROUND_KINDS.has(deployment.kind);
  const containerList = selectContainerList(containers.data, containers.hasNextPage);

  return (
    <WorkspacePage
      title={<span className="mono">{deployment.name}</span>}
      description={
        <PageFacts
          items={[
            kindFact,
            <span key="version" className="mono">
              v{deployment.version}
            </span>,
            isPublic ? "Public" : "Token required",
            countLabel(deployment.running_containers, "running", "running"),
          ]}
        />
      }
      actions={backLink}
      headerDetails={<WorkloadOperation workspace={workspace.name} workload={workload} />}
      contentClassName={
        isPod
          ? "flex flex-col overflow-y-auto lg:overflow-hidden"
          : "flex flex-col gap-3 overflow-y-auto xl:grid xl:grid-cols-[minmax(20rem,2fr)_minmax(0,3fr)] xl:overflow-hidden"
      }
    >
      <Tabs
        key={JSON.stringify([app, deployment.kind, deployment.name])}
        defaultValue={defaultInspectorTab(workload, showsInvoke)}
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
          {OBSERVABLE_KINDS.has(deployment.kind) ? <LinearTab value="call">Call</LinearTab> : null}
        </LinearTabsList>

        {showsInvoke ? (
          <TabsContent value="invoke" className="m-0 min-h-0 flex-1 overflow-hidden">
            <PanelErrorBoundary title="Invoke could not be displayed">
              <Playground workspace={workspace.name} workload={workload} />
            </PanelErrorBoundary>
          </TabsContent>
        ) : null}

        {isPod ? (
          <TabsContent value="instances" className="m-0 min-h-0 flex-1 overflow-hidden">
            <PodInstances
              workspace={workspace.name}
              deployment={deployment}
              statusFilter={podInstanceStatus}
              onStatusFilterChange={setPodInstanceStatus}
              containers={containerList.items}
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
          <VersionHistory workspace={workspace.name} workload={deployment} />
        </TabsContent>

        <TabsContent
          value="configuration"
          className={`m-0 min-h-0 flex-1 overflow-auto ${isPod ? "" : "max-xl:flex-none"}`}
        >
          <WorkloadConfiguration spec={release.spec} />
        </TabsContent>
        {OBSERVABLE_KINDS.has(deployment.kind) ? (
          <TabsContent value="call" className="m-0 min-h-0 flex-1 overflow-auto max-xl:flex-none">
            <PanelErrorBoundary title="Call methods could not be displayed">
              <CallMethods workspace={workspace.name} workload={workload} />
            </PanelErrorBoundary>
          </TabsContent>
        ) : null}
      </Tabs>

      {!isPod ? (
        <Panel
          title="Activity"
          contentClassName="flex flex-col overflow-hidden p-0"
          className="min-h-[24rem] shrink-0 xl:min-h-0"
        >
          <WorkloadLatency workspace={workspace.name} deployment={deployment} />
          <WorkloadRuns workspaceName={workspace.name} deployment={deployment} />
        </Panel>
      ) : null}
    </WorkspacePage>
  );
}

function WorkloadLatency({
  workspace,
  deployment,
}: {
  workspace: string;
  deployment: Schemas["DeployedWorkload"];
}) {
  const latency = useQuery(performanceQueryOptions(workspace, deployment.id));

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
          kind={deployment.kind}
        />
      </PanelErrorBoundary>
    </div>
  );
}

function WorkloadRuns({
  workspaceName,
  deployment,
}: {
  workspaceName: string;
  deployment: Schemas["DeployedWorkload"];
}) {
  const { app, kind, name } = deployment;
  const requests = servesRequests(kind);
  const tasks = useQuery({
    ...tasksQueryOptions(workspaceName, { app, function: name }),
    enabled: !requests,
  });
  const served = useQuery({ ...requestsQueryOptions(workspaceName, app, name), enabled: requests });
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
        params: { workspace: workspaceName, app, kind, name, taskId },
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

function defaultInspectorTab({ deployment, release }: Workload, showsInvoke: boolean): string {
  if (deployment.kind === "pod") return "instances";
  if (showsInvoke && !release.spec.cron) return "invoke";
  return "versions";
}

function kindLabel({ deployment, release }: Workload): string {
  // A scheduled function still reads as a schedule here: it is what the person
  // looking at the list is scanning for, even though it is a function.
  if (release.spec.cron) return "Schedule";
  if (deployment.role === "devbox") return "Devbox";
  const labels: Record<Schemas["WorkloadKind"], string> = {
    function: "Function",
    endpoint: "Endpoint",
    asgi: "ASGI",
    pod: "Pod",
    sandbox: "Sandbox",
  };
  return labels[deployment.kind];
}
