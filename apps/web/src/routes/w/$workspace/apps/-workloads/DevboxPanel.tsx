import { useQuery } from "@tanstack/react-query";

import { CopyButton } from "@/components/shared/CopyButton";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { Panel } from "@/components/shared/Panel";
import { Skeleton } from "@/components/ui/skeleton";
import type { Devbox, DevboxPhase } from "@/lib/api/schemas";
import { countLabel, formatBytes } from "@/lib/format";
import { deploymentDetailQueryOptions } from "@/lib/queries/deployments";

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

/** How to reach a devbox and what it is doing, rendered from the server's devbox block. */
export function DevboxPanel({
  workspaceId,
  deploymentId,
}: {
  workspaceId: string;
  deploymentId: string;
}) {
  const detail = useQuery(deploymentDetailQueryOptions(workspaceId, deploymentId));
  const devbox = detail.data?.devbox;

  return (
    <Panel title="Connect" className="mb-3 shrink-0" contentClassName="space-y-2 p-3">
      {detail.isError ? (
        <p className="text-xs text-destructive">{detail.error.message}</p>
      ) : !devbox ? (
        <div className="space-y-2" aria-hidden="true">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-4 w-2/3" />
        </div>
      ) : (
        <>
          <ConnectRow label="SSH" value={devbox.ssh_command} copyLabel="SSH command" prompt />
          <ConnectRow label="Editor host" value={devbox.ssh_host} copyLabel="SSH host" />
          <p className="text-[11px] text-muted-foreground">
            Editors find this host after <code className="mono">lazycloud ssh-config</code>.
          </p>
          <DevboxStatus devbox={devbox} />
        </>
      )}
    </Panel>
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
    <div className="grid min-w-0 grid-cols-[6rem_minmax(0,1fr)] items-center gap-3">
      <span className="text-xs text-muted-foreground">{label}</span>
      <div className="flex min-w-0 items-center justify-between gap-2 rounded-md border border-border bg-muted/50 px-3 py-1.5">
        <code className="mono truncate text-xs">
          {prompt ? "$ " : ""}
          {value}
        </code>
        <CopyButton value={value} label={copyLabel} className="size-7" />
      </div>
    </div>
  );
}

function DevboxStatus({ devbox }: { devbox: Devbox }) {
  const disk = devbox.disk;
  return (
    <p className="flex flex-wrap items-center gap-x-2 gap-y-1 pt-1 text-xs text-muted-foreground">
      <span className={devbox.phase === "failed" ? "text-destructive" : "text-foreground"}>
        {PHASE_LABELS[devbox.phase]}
        {devbox.phase === "failed" && devbox.phase_reason ? `: ${devbox.phase_reason}` : ""}
      </span>
      {devbox.state === "running" ? (
        <>
          <span aria-hidden="true">·</span>
          <span>{countLabel(devbox.open_connections, "connection", "connections")} open</span>
        </>
      ) : null}
      {devbox.idle_deadline ? (
        <>
          <span aria-hidden="true">·</span>
          <span>
            stops <LiveRelativeTime value={devbox.idle_deadline} /> unless something connects
          </span>
        </>
      ) : null}
      <span aria-hidden="true">·</span>
      {disk ? (
        <span>
          Disk <span className="mono">{disk.name}</span> {formatBytes(disk.size_bytes)},{" "}
          {formatBytes(disk.stored_bytes)} stored,{" "}
          {disk.generation > 0 ? `saved generation ${disk.generation}` : "not saved yet"}
        </span>
      ) : (
        <span>Disk created on first start</span>
      )}
    </p>
  );
}
