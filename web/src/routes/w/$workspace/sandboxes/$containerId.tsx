import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { Archive, Camera, Loader2, Square } from "lucide-react";

import { CopyId } from "@/components/shared/CopyId";
import { StopCause } from "@/components/shared/StopCause";
import { Fact } from "@/components/shared/Fact";
import { FactGrid } from "@/components/shared/Fact/FactGrid";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { ContainerFileBrowser } from "@/components/shared/ContainerFileBrowser";
import { PanelErrorBoundary, RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { Terminal } from "@/components/shared/Terminal";
import { LinearTab, LinearTabsList } from "@/components/shared/LinearSelect";
import { LiveDuration, LiveRelativeTime } from "@/components/shared/LiveTime";
import { Panel } from "@/components/shared/Panel";
import { StatusChip } from "@/components/shared/StatusChip";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import type { Schemas } from "@/lib/api/client";
import { containerQueryOptions, stopContainerMutationOptions } from "@/lib/queries/containers";
import {
  createSandboxImageMutationOptions,
  sandboxPortsQueryOptions,
  snapshotSandboxMemoryMutationOptions,
} from "@/lib/queries/sandboxes";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import { useWorkspace } from "@/lib/workspace-context";

import { ContainerLifecycle } from "./-components/ContainerLifecycle";
import { ContainerLineage } from "./-components/ContainerLineage";
import { SandboxProcessList } from "./-components/SandboxProcessList";

export const Route = createFileRoute("/w/$workspace/sandboxes/$containerId")({
  component: SandboxDetailPage,
  errorComponent: RouteErrorFallback,
});

function SandboxDetailPage() {
  const { containerId } = Route.useParams();
  const { workspace } = useWorkspace();
  const queryClient = useQueryClient();
  const container = useQuery(containerQueryOptions(workspace.name, containerId));
  const stop = useMutation({
    ...stopContainerMutationOptions(workspace.name, containerId),
    onSuccess: (stopped) =>
      queryClient.setQueryData(
        workspaceQueryKeys.containers.detail(workspace.name, containerId),
        stopped,
      ),
  });

  if (container.isPending) return <SandboxSkeleton />;
  if (container.isError) {
    return <PanelError message={container.error.message} layout="centered" />;
  }

  const record = container.data;
  const running = record.state === "ready";

  return (
    <WorkspacePage
      title={<span className="mono">{record.function}</span>}
      description={<ContainerLineage record={record} workspaceName={workspace.name} />}
      actions={
        <>
          <StatusChip status={record.state} live={running} />
          <SandboxActions record={record} onStop={() => stop.mutate()} stopping={stop.isPending} />
          {stop.isError ? <p className="text-xs text-destructive">{stop.error.message}</p> : null}
        </>
      }
      contentClassName="overflow-y-auto lg:overflow-hidden"
    >
      <div className="grid min-h-full gap-4 lg:h-full lg:min-h-0 lg:grid-rows-[auto_minmax(0,1fr)]">
        <div className="grid shrink-0 gap-4 lg:grid-cols-[minmax(0,1.3fr)_minmax(20rem,0.7fr)]">
          <Panel title="Lifecycle" contentClassName="px-4 py-3">
            <ContainerLifecycle
              createdAt={record.created_at}
              startedAt={record.ready_at}
              finishedAt={record.stopped_at}
              running={running}
            />
          </Panel>
          <Panel title="Sandbox" contentClassName="p-4">
            <SandboxFacts record={record} />
          </Panel>
        </div>

        <Tabs
          defaultValue="terminal"
          className="panel flex min-h-[36rem] flex-col overflow-hidden rounded-md lg:min-h-0"
        >
          <LinearTabsList
            ariaLabel="Sandbox inspector views"
            className="min-h-11 shrink-0 bg-card px-2"
          >
            <LinearTab value="terminal">Terminal</LinearTab>
            <LinearTab value="files">Files</LinearTab>
            <LinearTab value="processes">Processes</LinearTab>
            <LinearTab value="network">Network</LinearTab>
          </LinearTabsList>
          <TabsContent value="terminal" className="m-0 min-h-0 flex-1 overflow-hidden p-3">
            {running ? (
              <PanelErrorBoundary key={containerId} title="Terminal could not be displayed">
                <Terminal
                  workspace={workspace.name}
                  containerId={containerId}
                  className="h-full min-h-[28rem] lg:min-h-0"
                />
              </PanelErrorBoundary>
            ) : (
              <PanelEmpty message="Sandbox is not running" className="h-56" />
            )}
          </TabsContent>
          <TabsContent value="files" className="m-0 min-h-0 flex-1 overflow-hidden p-3">
            {running ? (
              <PanelErrorBoundary key={containerId} title="Files could not be displayed">
                <ContainerFileBrowser
                  containerId={containerId}
                  rootPath="/workspace"
                  writable
                  className="h-full"
                />
              </PanelErrorBoundary>
            ) : (
              <PanelEmpty message="Sandbox is not running" className="h-56" />
            )}
          </TabsContent>
          <TabsContent value="processes" className="m-0 min-h-0 flex-1 overflow-hidden p-3">
            {running ? (
              <PanelErrorBoundary key={containerId} title="Processes could not be displayed">
                <SandboxProcessList containerId={containerId} writable className="h-full" />
              </PanelErrorBoundary>
            ) : (
              <PanelEmpty message="Sandbox is not running" className="h-56" />
            )}
          </TabsContent>
          <TabsContent value="network" className="m-0 min-h-0 flex-1 overflow-auto p-3">
            <PanelErrorBoundary key={containerId} title="Network details could not be displayed">
              <SandboxNetwork record={record} workspace={workspace.name} />
            </PanelErrorBoundary>
          </TabsContent>
        </Tabs>
      </div>
    </WorkspacePage>
  );
}

function SandboxActions({
  record,
  onStop,
  stopping,
}: {
  record: Schemas["Container"];
  onStop: () => void;
  stopping: boolean;
}) {
  const { workspace } = useWorkspace();
  const image = useMutation(createSandboxImageMutationOptions(workspace.name, record.id));
  const memory = useMutation(snapshotSandboxMemoryMutationOptions(workspace.name, record.id));
  const actionError = image.error || memory.error;
  const running = record.state === "ready";
  return (
    <div className="flex max-w-full flex-wrap justify-end gap-2">
      {running ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={image.isPending || memory.isPending}
          onClick={() => image.mutate()}
        >
          {image.isPending ? <Loader2 className="animate-spin" /> : <Archive />}
          Save image
        </Button>
      ) : null}
      {running ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={image.isPending || memory.isPending}
          onClick={() => memory.mutate()}
        >
          {memory.isPending ? <Loader2 className="animate-spin" /> : <Camera />}
          Snapshot memory
        </Button>
      ) : null}
      {record.state !== "stopped" ? (
        <Button type="button" variant="destructive" size="sm" disabled={stopping} onClick={onStop}>
          <Square className="fill-current" />
          {stopping ? "Stopping" : "Stop"}
        </Button>
      ) : null}
      {image.data ? (
        <span className="flex basis-full items-center justify-end gap-1 text-xs text-muted-foreground">
          Image <CopyId value={image.data.image_id} />
        </span>
      ) : null}
      {memory.data ? (
        <span className="flex basis-full items-center justify-end gap-1 text-xs text-muted-foreground">
          Checkpoint <CopyId value={memory.data.id} />
        </span>
      ) : null}
      {actionError ? (
        <span className="basis-full text-right text-xs text-destructive">
          {actionError.message}
        </span>
      ) : null}
    </div>
  );
}

function SandboxFacts({ record }: { record: Schemas["Container"] }) {
  return (
    <>
      <FactGrid columns={2} className="gap-x-5 gap-y-3 text-xs">
        <Fact label="Image" value={record.image ?? "None"} mono />
        <Fact
          label="Uptime"
          value={
            <LiveDuration
              startedAt={record.ready_at}
              finishedAt={record.stopped_at}
              fallback="None"
            />
          }
        />
        <Fact
          label="Expires"
          value={record.expires_at ? <LiveRelativeTime value={record.expires_at} /> : "No expiry"}
        />
        <Fact
          label="Version"
          value={record.version === undefined ? "None" : `v${record.version}`}
          mono
        />
        <Fact label="Created" value={<LiveRelativeTime value={record.created_at} />} />
        <Fact label="Container" value={<CopyId value={record.id} className="-ml-1.5" />} />
      </FactGrid>
      <StopCause
        reason={record.stop_reason}
        message={record.exit_message}
        className="mt-3 text-xs"
      />
    </>
  );
}

function SandboxNetwork({
  record,
  workspace,
}: {
  record: Schemas["Container"];
  workspace: string;
}) {
  const running = record.state === "ready";
  const ports = useQuery(sandboxPortsQueryOptions(workspace, record.id, running));

  if (!running) return <PanelEmpty message="Sandbox is not running" className="h-56" />;
  if (ports.isPending) {
    return <Skeleton className="h-24 w-full" />;
  }
  if (ports.isError) {
    return <PanelError message={ports.error.message} />;
  }
  const exposed = [...ports.data].sort((a, b) => a.port - b.port);
  return (
    <div className="content-transition h-full min-h-0 overflow-auto">
      <div className="sticky top-0 grid grid-cols-[5rem_minmax(0,1fr)] border-b border-border bg-card px-3 py-2 text-[11px] uppercase text-muted-foreground sm:grid-cols-[7rem_minmax(0,1fr)]">
        <span>Port</span>
        <span>Endpoint</span>
      </div>
      {exposed.length ? (
        exposed.map(({ port, url }) => (
          <div
            key={port}
            className="grid grid-cols-[5rem_minmax(0,1fr)] border-b border-border/60 px-3 py-2 text-xs last:border-0 sm:grid-cols-[7rem_minmax(0,1fr)]"
          >
            <span className="mono">{port}</span>
            <a
              href={url}
              target="_blank"
              rel="noreferrer"
              className="mono min-w-0 truncate text-brand hover:underline"
            >
              {url}
            </a>
          </div>
        ))
      ) : (
        <PanelEmpty message="No exposed ports" className="p-4" />
      )}
    </div>
  );
}

function SandboxSkeleton() {
  return (
    <WorkspacePage pending title={<Skeleton className="h-7 w-72" />}>
      <div
        className="grid h-full min-h-0 gap-4 lg:grid-rows-[8rem_minmax(0,1fr)]"
        aria-hidden="true"
      >
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1.3fr)_minmax(20rem,0.7fr)]">
          <Skeleton className="h-full min-h-32 w-full" />
          <Skeleton className="h-full min-h-32 w-full" />
        </div>
        <Skeleton className="h-full min-h-[32rem] w-full lg:min-h-0" />
      </div>
    </WorkspacePage>
  );
}
