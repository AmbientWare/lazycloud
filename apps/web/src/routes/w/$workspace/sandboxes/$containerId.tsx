import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { Archive, Camera, Loader2, Square } from "lucide-react";

import { CopyId } from "@/components/shared/CopyId";
import { PanelErrorBoundary, RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { LinearTab, LinearTabsList } from "@/components/shared/LinearSelect";
import { Panel } from "@/components/shared/Panel";
import { StatusChip } from "@/components/shared/StatusChip";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import type { ContainerDetail } from "@/lib/api/schemas";
import { durationBetween, relativeTime } from "@/lib/format";
import { containerQueryOptions, stopContainerMutationOptions } from "@/lib/queries/containers";
import {
  createSandboxImageMutationOptions,
  sandboxUrlsQueryOptions,
  snapshotSandboxMemoryMutationOptions,
} from "@/lib/queries/sandboxes";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import { useWorkspace } from "@/lib/workspace-context";

import { ContainerLifecycle } from "./-components/ContainerLifecycle";
import { ContainerLineage } from "./-components/ContainerLineage";
import { SandboxFileBrowser } from "./-components/SandboxFileBrowser";
import { SandboxProcessList } from "./-components/SandboxProcessList";
import { SandboxTerminal } from "./-components/SandboxTerminal";

export const Route = createFileRoute("/w/$workspace/sandboxes/$containerId")({
  component: SandboxDetailPage,
  errorComponent: RouteErrorFallback,
});

function SandboxDetailPage() {
  const { containerId } = Route.useParams();
  const { workspace } = useWorkspace();
  const queryClient = useQueryClient();
  const container = useQuery(containerQueryOptions(workspace.id, containerId));
  const stop = useMutation({
    ...stopContainerMutationOptions(workspace.id, containerId),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.containers.detail(workspace.id, containerId),
      }),
  });

  if (container.isPending) return <SandboxSkeleton />;
  if (container.isError) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-destructive">
        {container.error.message}
      </div>
    );
  }

  const record = container.data;
  const running = record.status === "running";

  return (
    <WorkspacePage
      title={<span className="mono">{record.name}</span>}
      description={<ContainerLineage record={record} workspaceName={workspace.name} />}
      actions={
        <>
          <StatusChip status={record.status} live={running} />
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
              startedAt={record.started_at}
              finishedAt={record.finished_at}
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
            {record.actions.can_shell ? (
              <PanelErrorBoundary key={containerId} title="Terminal could not be displayed">
                <SandboxTerminal
                  containerId={containerId}
                  className="h-full min-h-[28rem] lg:min-h-0"
                />
              </PanelErrorBoundary>
            ) : (
              <EmptyOperation
                message={running ? "Shell access is unavailable" : "Sandbox is not running"}
              />
            )}
          </TabsContent>
          <TabsContent value="files" className="m-0 min-h-0 flex-1 overflow-hidden p-3">
            {running ? (
              <PanelErrorBoundary key={containerId} title="Files could not be displayed">
                <SandboxFileBrowser containerId={containerId} writable className="h-full" />
              </PanelErrorBoundary>
            ) : (
              <EmptyOperation message="Sandbox is not running" />
            )}
          </TabsContent>
          <TabsContent value="processes" className="m-0 min-h-0 flex-1 overflow-hidden p-3">
            {running ? (
              <PanelErrorBoundary key={containerId} title="Processes could not be displayed">
                <SandboxProcessList containerId={containerId} writable className="h-full" />
              </PanelErrorBoundary>
            ) : (
              <EmptyOperation message="Sandbox is not running" />
            )}
          </TabsContent>
          <TabsContent value="network" className="m-0 min-h-0 flex-1 overflow-auto p-3">
            <PanelErrorBoundary key={containerId} title="Network details could not be displayed">
              <SandboxNetwork record={record} workspaceId={workspace.id} />
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
  record: ContainerDetail;
  onStop: () => void;
  stopping: boolean;
}) {
  const { workspace } = useWorkspace();
  const stubId = record.workload?.id ?? "";
  const image = useMutation(createSandboxImageMutationOptions(workspace.id, record.id, stubId));
  const memory = useMutation(snapshotSandboxMemoryMutationOptions(workspace.id, record.id, stubId));
  const actionError = image.error || memory.error;
  return (
    <div className="flex max-w-full flex-wrap justify-end gap-2">
      {record.actions.can_create_image ? (
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
      {record.actions.can_snapshot_memory ? (
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
      {record.actions.can_stop ? (
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
          Checkpoint <CopyId value={memory.data.checkpoint_id} />
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

function SandboxFacts({ record }: { record: ContainerDetail }) {
  return (
    <dl className="grid grid-cols-2 gap-x-5 gap-y-3 text-xs">
      <Fact label="Image" value={record.image} mono />
      <Fact
        label="Uptime"
        value={durationBetween(record.started_at, record.finished_at) ?? "None"}
      />
      <Fact
        label="Expires"
        value={record.expires_at ? relativeTime(record.expires_at) : "No expiry"}
      />
      <Fact
        label="Version"
        value={record.deployment ? `v${record.deployment.version}` : "None"}
        mono
      />
      <Fact label="Created" value={relativeTime(record.created_at)} />
      <div>
        <dt className="micro-label mb-1">Container</dt>
        <dd className="-ml-1.5">
          <CopyId value={record.id} />
        </dd>
      </div>
    </dl>
  );
}

function Fact({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="micro-label mb-1">{label}</dt>
      <dd className={mono ? "mono truncate" : "truncate tabular-nums"} title={value}>
        {value}
      </dd>
    </div>
  );
}

function SandboxNetwork({ record, workspaceId }: { record: ContainerDetail; workspaceId: string }) {
  const running = record.status === "running";
  const urls = useQuery(sandboxUrlsQueryOptions(workspaceId, record.id, running));
  const configuredPorts = [...new Set(Object.values(record.ports))].sort((a, b) => a - b);

  if (!running) return <EmptyOperation message="Sandbox is not running" />;
  if (urls.isPending) {
    return <Skeleton className="h-24 w-full" />;
  }
  if (urls.isError) {
    return <p className="text-sm text-destructive">{urls.error.message}</p>;
  }
  const exposed = Object.entries(urls.data.urls).sort(([a], [b]) => Number(a) - Number(b));
  return (
    <div className="h-full min-h-0 overflow-auto">
      <div className="sticky top-0 grid grid-cols-[5rem_minmax(0,1fr)] border-b border-border bg-card px-3 py-2 text-[11px] uppercase text-muted-foreground sm:grid-cols-[7rem_minmax(0,1fr)]">
        <span>Port</span>
        <span>Endpoint</span>
      </div>
      {exposed.length ? (
        exposed.map(([port, url]) => (
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
      ) : configuredPorts.length ? (
        configuredPorts.map((port) => (
          <div
            key={port}
            className="grid grid-cols-[5rem_minmax(0,1fr)] border-b border-border/60 px-3 py-2 text-xs last:border-0 sm:grid-cols-[7rem_minmax(0,1fr)]"
          >
            <span className="mono">{port}</span>
            <span className="text-muted-foreground">Not published</span>
          </div>
        ))
      ) : (
        <p className="p-4 text-sm text-muted-foreground">No exposed ports</p>
      )}
    </div>
  );
}

function EmptyOperation({ message }: { message: string }) {
  return (
    <div className="flex h-56 items-center justify-center text-sm text-muted-foreground">
      {message}
    </div>
  );
}

function SandboxSkeleton() {
  return (
    <WorkspacePage title={<Skeleton className="h-7 w-72" />}>
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
