import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

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
import { LogViewer } from "@/components/shared/TaskDrawer/LogViewer";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import type { Devbox, DevboxPhase, DiskStatus } from "@/lib/api/schemas";
import { formatBytes } from "@/lib/format";
import { containerMetricsTimeseriesQueryOptions } from "@/lib/queries/containers";
import { devboxQueryOptions } from "@/lib/queries/deployments";

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

const DISK_STATUS_LABELS: Record<DiskStatus, string> = {
  detached: "detached",
  attached: "attached",
  saving: "saving",
  deleting: "being deleted",
};

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
        <ConnectRow label="Editor host" value={devbox.ssh_host} copyLabel="SSH host" />
        <p className="text-[11px] text-muted-foreground">
          Editors find this host after <code className="mono">lazycloud ssh-config</code>.
        </p>
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
        {devbox.disk ? (
          <p className="text-xs text-muted-foreground">
            Disk <span className="mono text-foreground">{devbox.disk.name}</span> is{" "}
            {DISK_STATUS_LABELS[devbox.disk.status]},{" "}
            {devbox.disk.generation > 0
              ? `${formatBytes(devbox.disk.stored_bytes)} saved in generation ${devbox.disk.generation.toLocaleString()}.`
              : "not saved yet."}
          </p>
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
    <div className="grid min-w-0 grid-cols-[5.5rem_minmax(0,1fr)] items-center gap-3">
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

/** Files, logs, versions and configuration beside the devbox's metrics. */
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
  const logContainerId = devbox?.container_id ?? devbox?.failed_container_id ?? null;

  return (
    <>
      <Tabs
        key={deploymentId}
        defaultValue="files"
        className="panel flex min-h-[32rem] flex-col overflow-hidden rounded-md xl:min-h-0"
      >
        <LinearTabsList ariaLabel="Devbox views" className="min-h-11 shrink-0 bg-card px-2">
          <LinearTab value="files">Files</LinearTab>
          <LinearTab value="logs">Logs</LinearTab>
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
                devbox.phase === "stopped" || devbox.phase === "failed" ? (
                  <>
                    <code className="mono">{devbox.ssh_command}</code> starts it.
                  </>
                ) : undefined
              }
              className="h-56"
            />
          )}
        </TabsContent>
        <TabsContent value="logs" className="m-0 flex min-h-0 flex-1 flex-col overflow-hidden">
          {status.isError ? (
            <PanelError message={status.error.message} />
          ) : !devbox ? (
            <Skeleton className="m-3 h-40" aria-hidden="true" />
          ) : logContainerId ? (
            <>
              <div className="flex min-h-9 shrink-0 items-center justify-end border-b border-border/80 px-3 text-xs">
                <Link
                  to="/w/$workspace/apps/$appId/workloads/$kind/$name/instances/$containerId"
                  params={{
                    workspace: workspaceName,
                    appId,
                    kind: group.kind,
                    name: group.name,
                    containerId: logContainerId,
                  }}
                  className="interactive-link text-muted-foreground hover:text-foreground"
                >
                  Container details
                </Link>
              </div>
              <PanelErrorBoundary key={logContainerId} title="Logs could not be displayed">
                <LogViewer
                  workspaceId={workspaceId}
                  scope={{ containerId: logContainerId }}
                  className="min-h-0 flex-1"
                />
              </PanelErrorBoundary>
            </>
          ) : (
            <PanelEmpty message="Logs appear once the devbox starts" className="h-56" />
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
