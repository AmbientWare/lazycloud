import type { ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Play, Square } from "lucide-react";

import { ContainerFileBrowser } from "@/components/shared/ContainerFileBrowser";
import { ChartSkeleton, ContainerMetricsCharts } from "@/components/shared/ContainerMetricsCharts";
import { CopyButton } from "@/components/shared/CopyButton";
import { PanelErrorBoundary } from "@/components/shared/ErrorBoundary";
import { Fact } from "@/components/shared/Fact";
import { FactGrid } from "@/components/shared/Fact/FactGrid";
import { LinearTab, LinearTabsList } from "@/components/shared/LinearSelect";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { Panel } from "@/components/shared/Panel";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { ShellButton } from "@/components/shared/ShellDialog";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import type { Deployment, Devbox, DevboxPhase } from "@/lib/api/schemas";
import { formatBytes } from "@/lib/format";
import { startDeploymentMutationOptions, stopDeploymentMutationOptions } from "@/lib/queries/apps";
import { containerMetricsTimeseriesQueryOptions } from "@/lib/queries/containers";
import { devboxQueryOptions } from "@/lib/queries/deployments";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

import type { WorkloadGroup } from "./grouping";
import { VersionHistory } from "./VersionHistory";
import { WorkloadConfiguration } from "./WorkloadConfiguration";

const PHASE_LABELS: Record<DevboxPhase, string> = {
  stopped: "Stopped",
  queued: "Waiting for a machine",
  pulling_image: "Pulling image",
  restoring_disk: "Restoring disk",
  starting: "Starting",
  running: "Running",
  stopping: "Saving disk",
  failed: "Start failed",
};

/**
 * Shell, Start and Stop for the devbox header.
 *
 * Start and Stop switch the deployment on and off. Stop also stops the
 * container; a started devbox still boots on its first connection, so Start is
 * offered only while the deployment is off.
 */
export function DevboxActions({
  workspaceId,
  appId,
  deployment,
}: {
  workspaceId: string;
  appId: string;
  deployment: Deployment;
}) {
  const queryClient = useQueryClient();
  const status = useQuery(devboxQueryOptions(workspaceId, deployment.id));
  const devbox = status.data;
  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.deployments.devbox(workspaceId, deployment.id),
      }),
      queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.deployments.root(workspaceId) }),
      queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.apps.detail(workspaceId, appId),
      }),
      queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.apps.summaries(workspaceId) }),
    ]);
  };
  const start = useMutation({
    ...startDeploymentMutationOptions(workspaceId, deployment.id),
    onSettled: refresh,
  });
  const stop = useMutation({
    ...stopDeploymentMutationOptions(workspaceId, deployment.id),
    onSettled: refresh,
  });
  if (!devbox) return null;

  const running = devbox.state === "running";
  const busy = start.isPending || stop.isPending || devbox.phase === "stopping";
  const error = start.error ?? stop.error;

  return (
    <>
      {error ? (
        <p className="text-xs text-destructive" role="alert">
          {error.message}
        </p>
      ) : null}
      <ShellButton
        containerId={devbox.container_id}
        running={running}
        disabledReason="Devbox is not running"
      />
      {devbox.state !== "stopped" && deployment.actions.can_stop ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => stop.mutate()}
        >
          <Square className="fill-current" />
          {stop.isPending ? "Stopping" : "Stop"}
        </Button>
      ) : deployment.actions.can_start ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => start.mutate()}
        >
          <Play />
          {start.isPending ? "Starting" : "Start"}
        </Button>
      ) : null}
    </>
  );
}

/** How to reach a devbox and what it is doing, from the server's devbox status. */
export function DevboxConnect({
  workspaceId,
  deploymentId,
}: {
  workspaceId: string;
  deploymentId: string;
}) {
  const status = useQuery(devboxQueryOptions(workspaceId, deploymentId));
  const devbox = status.data;

  if (status.isError) return <PanelError message={status.error.message} />;
  if (!devbox) {
    return (
      <div className="grid gap-3 xl:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]" aria-hidden="true">
        <div className="space-y-2">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
        </div>
        <Skeleton className="h-12 w-full" />
      </div>
    );
  }

  return (
    <div className="content-transition grid min-w-0 gap-x-8 gap-y-4 xl:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
      <div className="min-w-0 space-y-2">
        <ConnectRow label="SSH" value={devbox.ssh_command} copyLabel="SSH command" prompt />
        <ConnectRow label="SSH config host" value={devbox.ssh_host} copyLabel="SSH host" />
      </div>
      <div className="min-w-0 space-y-3">
        <FactGrid columns={4}>
          <Fact
            label="Status"
            value={
              <span className={devbox.phase === "failed" ? "text-destructive" : undefined}>
                {PHASE_LABELS[devbox.phase]}
              </span>
            }
          />
          <Fact
            label="Connections"
            value={devbox.state === "running" ? `${devbox.open_connections} open` : "None"}
          />
          <Fact label="Idle stop" value={idleStop(devbox)} />
          <Fact
            label="Disk"
            value={devbox.disk ? formatBytes(devbox.disk.size_bytes) : "Created on first start"}
          />
        </FactGrid>
        {devbox.phase === "failed" && devbox.phase_reason ? (
          <p className="text-xs break-words text-destructive">{devbox.phase_reason}</p>
        ) : null}
      </div>
    </div>
  );
}

function ConnectRow({
  label,
  value,
  copyLabel,
  prompt = false,
}: {
  label: string;
  value: string;
  copyLabel: string;
  prompt?: boolean;
}) {
  return (
    <div className="grid min-w-0 grid-cols-[6.5rem_minmax(0,1fr)] items-center gap-3">
      <span className="text-xs text-muted-foreground">{label}</span>
      <div className="flex min-w-0 items-center justify-between gap-2 rounded-md border border-border bg-muted/50 py-1 pr-1 pl-3">
        <code className="mono truncate text-xs" title={value}>
          {prompt ? "$ " : ""}
          {value}
        </code>
        <CopyButton value={value} label={copyLabel} className="size-7 shrink-0" />
      </div>
    </div>
  );
}

function idleStop(devbox: Devbox): ReactNode {
  if (devbox.idle_deadline) return <LiveRelativeTime value={devbox.idle_deadline} />;
  if (devbox.state === "running" && devbox.open_connections > 0) return "Not while connected";
  return "None";
}

/** Files, versions and configuration beside the devbox's metrics. */
export function DevboxWorkspace({
  workspaceId,
  workspaceName,
  appId,
  group,
  deploymentId,
  nextCursor,
  loadingMore,
  loadMoreError,
  onLoadMore,
}: {
  workspaceId: string;
  workspaceName: string;
  appId: string;
  group: WorkloadGroup;
  deploymentId: string;
  nextCursor: string | undefined;
  loadingMore: boolean;
  loadMoreError: boolean;
  onLoadMore: () => void;
}) {
  const status = useQuery(devboxQueryOptions(workspaceId, deploymentId));
  const devbox = status.data;
  const current = group.deployments.find((deployment) => deployment.id === deploymentId);

  return (
    <>
      <Tabs
        key={deploymentId}
        defaultValue="files"
        className="panel flex min-h-[32rem] flex-col overflow-hidden rounded-md xl:min-h-0"
      >
        <LinearTabsList ariaLabel="Devbox views" className="min-h-11 shrink-0 bg-card px-2">
          <LinearTab value="files">Files</LinearTab>
          <LinearTab value="versions">Versions</LinearTab>
          <LinearTab value="configuration">Configuration</LinearTab>
        </LinearTabsList>
        <TabsContent value="files" className="m-0 min-h-0 flex-1 overflow-hidden">
          {status.isError ? (
            <PanelError message={status.error.message} />
          ) : !devbox ? (
            <Skeleton className="m-3 h-40" aria-hidden="true" />
          ) : devbox.state === "running" && devbox.container_id ? (
            <PanelErrorBoundary key={devbox.container_id} title="Files could not be displayed">
              <ContainerFileBrowser
                containerId={devbox.container_id}
                rootPath="/"
                writable={false}
                className="h-full"
              />
            </PanelErrorBoundary>
          ) : (
            <PanelEmpty
              message={notRunningMessage(devbox)}
              detail={
                current?.active && (devbox.phase === "stopped" || devbox.phase === "failed") ? (
                  <>
                    <code className="mono">{devbox.ssh_command}</code> starts it.
                  </>
                ) : undefined
              }
              className="h-56"
            />
          )}
        </TabsContent>
        <TabsContent value="versions" className="m-0 min-h-0 flex-1 overflow-auto">
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
        </TabsContent>
        <TabsContent value="configuration" className="m-0 min-h-0 flex-1 overflow-auto">
          {current ? <WorkloadConfiguration deployment={current} kind={group.kind} /> : null}
        </TabsContent>
      </Tabs>

      <Panel
        title="Metrics"
        className="min-h-[24rem] shrink-0 xl:min-h-0"
        contentClassName="p-4"
        action={
          devbox?.container_id ? <MetricsUpdated workspaceId={workspaceId} devbox={devbox} /> : null
        }
      >
        {devbox?.container_id ? (
          <DevboxMetrics
            workspaceId={workspaceId}
            containerId={devbox.container_id}
            live={devbox.state === "running"}
          />
        ) : devbox ? (
          <PanelEmpty message="Metrics appear while the devbox runs" className="h-44" />
        ) : (
          <ChartSkeleton />
        )}
      </Panel>
    </>
  );
}

function DevboxMetrics({
  workspaceId,
  containerId,
  live,
}: {
  workspaceId: string;
  containerId: string;
  live: boolean;
}) {
  const metrics = useQuery(containerMetricsTimeseriesQueryOptions(workspaceId, containerId, live));
  if (metrics.isPending) {
    return (
      <div className="grid gap-y-5">
        <ChartSkeleton />
        <ChartSkeleton />
      </div>
    );
  }
  if (metrics.isError) return <PanelError message={metrics.error.message} layout="centered" />;
  return (
    <PanelErrorBoundary title="Metrics could not be displayed">
      <ContainerMetricsCharts
        points={metrics.data.points}
        showIo={false}
        className="lg:grid-cols-1"
      />
    </PanelErrorBoundary>
  );
}

function MetricsUpdated({ workspaceId, devbox }: { workspaceId: string; devbox: Devbox }) {
  const metrics = useQuery(
    containerMetricsTimeseriesQueryOptions(
      workspaceId,
      devbox.container_id ?? "",
      devbox.state === "running",
    ),
  );
  const latest = metrics.data?.points.at(-1)?.timestamp;
  if (!latest) return null;
  return (
    <span className="text-[11px] text-muted-foreground">
      Updated <LiveRelativeTime value={latest} />
    </span>
  );
}

function notRunningMessage(devbox: Devbox): string {
  if (devbox.state === "starting") return "Files appear once it is running";
  if (devbox.phase === "failed") return "The last start failed";
  return "Files appear while the devbox runs";
}
