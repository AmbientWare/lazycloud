import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Play, Square } from "lucide-react";

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
import { DrawerHeader } from "@/components/shared/DrawerHeader";
import { ShellButton } from "@/components/shared/ShellDialog";
import { LogViewer } from "@/components/shared/TaskDrawer/LogViewer";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import type { Devbox, DevboxPhase } from "@/lib/api/schemas";
import { formatBytes } from "@/lib/format";
import { containerMetricsTimeseriesQueryOptions } from "@/lib/queries/containers";
import {
  devboxQueryOptions,
  startDevboxMutationOptions,
  stopDevboxMutationOptions,
} from "@/lib/queries/deployments";
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

type DevboxAction =
  { kind: "start" } | { kind: "stop" } | { kind: "busy"; label: string; reason: string };

/**
 * The one power action the header offers, from the server's state and what the
 * person asked for. A start is followed by the server's own `starting` state; a
 * stop is not visible in it until the container is gone, so the request is held
 * locally until the server reports the devbox stopped.
 */
function devboxAction(
  devbox: Devbox,
  request: { starting: boolean; stopping: boolean },
): DevboxAction {
  if (request.stopping) {
    return { kind: "busy", label: "Stopping…", reason: "Stopping the container" };
  }
  if (devbox.phase === "stopping") {
    return {
      kind: "busy",
      label: "Stopping…",
      reason: "Saving disk. Start is available once it is saved.",
    };
  }
  if (devbox.state === "starting") {
    return { kind: "busy", label: "Starting…", reason: PHASE_LABELS[devbox.phase] };
  }
  if (request.starting) {
    return { kind: "busy", label: "Starting…", reason: "Asking for a machine" };
  }
  return devbox.state === "running" ? { kind: "stop" } : { kind: "start" };
}

/** Shell, Start and Stop for the devbox header. */
export function DevboxActions({
  workspaceId,
  deploymentId,
}: {
  workspaceId: string;
  deploymentId: string;
}) {
  const queryClient = useQueryClient();
  const [stopRequested, setStopRequested] = useState(false);
  const status = useQuery(
    devboxQueryOptions(workspaceId, deploymentId, { awaitingChange: stopRequested }),
  );
  const devbox = status.data;
  const key = workspaceQueryKeys.deployments.devbox(workspaceId, deploymentId);
  // A poll that left before the request could land after its answer and undo it.
  const cancelPolls = () => queryClient.cancelQueries({ queryKey: key });
  const start = useMutation({
    ...startDevboxMutationOptions(workspaceId, deploymentId),
    onMutate: cancelPolls,
    onSuccess: async (next) => {
      queryClient.setQueryData(key, next);
      // A start switches a stopped deployment back on.
      await queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.deployments.root(workspaceId),
      });
    },
    onError: () => queryClient.invalidateQueries({ queryKey: key }),
  });
  const stop = useMutation({
    ...stopDevboxMutationOptions(workspaceId, deploymentId),
    onMutate: cancelPolls,
    onSuccess: (next) => queryClient.setQueryData(key, next),
    onError: () => {
      setStopRequested(false);
      return queryClient.invalidateQueries({ queryKey: key });
    },
  });
  if (stopRequested && !stop.isPending && devbox?.state === "stopped") {
    setStopRequested(false);
  }

  if (status.isPending) {
    return (
      <div className="flex items-center gap-2" aria-hidden="true">
        <Skeleton className="h-8 w-20" />
        <Skeleton className="h-8 w-24" />
      </div>
    );
  }
  if (!devbox) return null;

  const action = devboxAction(devbox, {
    starting: start.isPending,
    stopping: stop.isPending || stopRequested,
  });
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
        running={devbox.state === "running"}
        disabledReason={
          devbox.state === "starting"
            ? "Shell opens once the devbox is running"
            : "Start the devbox to open a shell"
        }
      />
      {action.kind === "busy" ? (
        // A disabled button takes no pointer events, so the wrapper carries its reason.
        <span className="inline-flex" title={action.reason}>
          <Button type="button" variant="outline" size="sm" className="min-w-26" disabled>
            <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
            {action.label}
            <span className="sr-only">{action.reason}</span>
          </Button>
        </span>
      ) : action.kind === "start" ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="min-w-26"
          onClick={() => {
            stop.reset();
            start.mutate();
          }}
        >
          <Play />
          Start
        </Button>
      ) : (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="min-w-26"
          onClick={() => {
            start.reset();
            setStopRequested(true);
            stop.mutate();
          }}
        >
          <Square className="fill-current" />
          Stop
        </Button>
      )}
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
        {devbox.phase === "failed" && (devbox.phase_reason || devbox.failed_container_id) ? (
          <div className="flex min-w-0 items-start gap-3 text-xs">
            {devbox.phase_reason ? (
              <p className="min-w-0 break-words text-destructive">{devbox.phase_reason}</p>
            ) : null}
            {devbox.failed_container_id ? (
              <StartLogs workspaceId={workspaceId} containerId={devbox.failed_container_id} />
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** The failed start's container logs, in a drawer over the page. */
function StartLogs({ workspaceId, containerId }: { workspaceId: string; containerId: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className="interactive-link shrink-0 text-muted-foreground hover:text-foreground"
        onClick={() => setOpen(true)}
      >
        View logs
      </button>
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent
          aria-describedby={undefined}
          className="gap-0 bg-background max-sm:left-0 max-sm:right-0 max-sm:max-w-none max-sm:border-l-0 sm:max-w-3xl xl:max-w-4xl"
        >
          <DrawerHeader className="flex min-h-14 items-center">
            <SheetTitle>Start logs</SheetTitle>
          </DrawerHeader>
          {open ? (
            <PanelErrorBoundary key={containerId} title="Logs could not be displayed">
              <LogViewer
                workspaceId={workspaceId}
                scope={{ containerId }}
                className="min-h-0 flex-1"
              />
            </PanelErrorBoundary>
          ) : null}
        </SheetContent>
      </Sheet>
    </>
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
