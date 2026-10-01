import { createFileRoute, Link, Outlet } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
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
import { stubId } from "@/lib/api/views";
import { countLabel } from "@/lib/format";
import {
  performanceQueryOptions,
  WorkloadNotFoundError,
  workloadQueryOptions,
  workloadRunning,
  type Workload,
} from "@/lib/queries/deployments";
import { tasksQueryOptions } from "@/lib/queries/tasks";
import { useWorkspace } from "@/lib/workspace-context";

import { CallMethods } from "./-workloads/CallMethods";
import { LatencyPanel, latencyHasSignal } from "./-workloads/LatencyPanel";
import { Playground } from "./-workloads/Playground";
import { PLAYGROUND_KINDS } from "./-workloads/playground-form";
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
  const query = useQuery(workloadQueryOptions(workspace.name, app, kind, name));

  if (query.isPending) return <WorkloadSkeleton />;
  if (query.error instanceof WorkloadNotFoundError) {
    return <PanelEmpty message={query.error.message} className="h-full" />;
  }
  if (query.isError) return <PanelError message={query.error.message} />;

  const workload = query.data;
  const { deployment, release } = workload;
  const isPublic = release.spec.authorized === false;
  const showsInvoke = workloadRunning(deployment) && PLAYGROUND_KINDS.has(deployment.kind);
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

  return (
    <WorkspacePage
      title={<span className="mono">{deployment.name}</span>}
      description={
        <PageFacts
          items={[
            <span key="kind" className="flex items-center gap-1.5">
              <StubKindIcon kind={deployment.kind} className="size-3.5" />
              {kindLabel(workload)}
            </span>,
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
      contentClassName="flex flex-col gap-3 overflow-y-auto xl:grid xl:grid-cols-[minmax(20rem,2fr)_minmax(0,3fr)] xl:overflow-hidden"
    >
      <Tabs
        key={JSON.stringify([app, deployment.kind, deployment.name])}
        defaultValue={showsInvoke && !release.spec.cron ? "invoke" : "versions"}
        className="panel flex flex-col overflow-hidden rounded-md shrink-0 xl:min-h-0"
      >
        <LinearTabsList
          ariaLabel="Workload inspector views"
          className="min-h-11 shrink-0 bg-card px-2"
        >
          {showsInvoke ? <LinearTab value="invoke">Invoke</LinearTab> : null}
          <LinearTab value="versions">Versions</LinearTab>
          <LinearTab value="configuration">Configuration</LinearTab>
          <LinearTab value="call">Call</LinearTab>
        </LinearTabsList>

        {showsInvoke ? (
          <TabsContent value="invoke" className="m-0 min-h-0 flex-1 overflow-hidden">
            <PanelErrorBoundary title="Invoke could not be displayed">
              <Playground workspace={workspace.name} workload={workload} />
            </PanelErrorBoundary>
          </TabsContent>
        ) : null}

        <TabsContent value="versions" className="m-0 min-h-0 flex-1 overflow-auto max-xl:flex-none">
          <VersionHistory workspace={workspace.name} workload={deployment} />
        </TabsContent>

        <TabsContent
          value="configuration"
          className="m-0 min-h-0 flex-1 overflow-auto max-xl:flex-none"
        >
          <WorkloadConfiguration spec={release.spec} />
        </TabsContent>
        <TabsContent value="call" className="m-0 min-h-0 flex-1 overflow-auto max-xl:flex-none">
          <PanelErrorBoundary title="Call methods could not be displayed">
            <CallMethods workspace={workspace.name} workload={workload} />
          </PanelErrorBoundary>
        </TabsContent>
      </Tabs>

      <Panel
        title="Activity"
        contentClassName="flex flex-col overflow-hidden p-0"
        className="min-h-[24rem] shrink-0 xl:min-h-0"
      >
        <WorkloadLatency workspace={workspace.name} deployment={deployment} />
        <WorkloadRuns
          workspaceId={workspace.id}
          workspaceName={workspace.name}
          deployment={deployment}
        />
      </Panel>
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
  workspaceId,
  workspaceName,
  deployment,
}: {
  workspaceId: string;
  workspaceName: string;
  deployment: Schemas["DeployedWorkload"];
}) {
  const { app, kind, name } = deployment;
  const tasks = useQuery(
    tasksQueryOptions(workspaceId, {
      limit: 50,
      appId: app,
      stubIds: [stubId(app, name, deployment.release_id ?? "")],
    }),
  );
  if (tasks.isError) {
    return <PanelError message={tasks.error.message} />;
  }
  return (
    <TaskTable
      tasks={tasks.isPending ? undefined : tasks.data?.data}
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

function kindLabel({ deployment, release }: Workload): string {
  // A scheduled function still reads as a schedule here: it is what the person
  // looking at the list is scanning for, even though it is a function.
  if (release.spec.cron) return "Schedule";
  const labels: Record<Schemas["WorkloadKind"], string> = {
    function: "Function",
    endpoint: "Endpoint",
    asgi: "ASGI",
  };
  return labels[deployment.kind];
}
