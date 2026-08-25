import { Link } from "@tanstack/react-router";
import { ArrowUpRight } from "lucide-react";

import { Panel } from "@/components/shared/Panel";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { RowsSkeleton } from "@/components/shared/RowsSkeleton";
import { StatusChip } from "@/components/shared/StatusChip";
import type { SandboxRow } from "@/lib/api/schemas";
import { countLabel, exactTime, formatDuration, relativeTime } from "@/lib/format";

export function AppSandboxesSection({
  workspaceName,
  sandboxes,
  pending,
  error,
}: {
  workspaceName: string;
  sandboxes: SandboxRow[] | undefined;
  pending: boolean;
  error: string | undefined;
}) {
  const running = sandboxes?.filter((sandbox) => sandbox.status === "running").length ?? 0;

  return (
    <div
      role="region"
      aria-labelledby="app-sandboxes-heading"
      className="min-h-[20rem] lg:h-full lg:min-h-0"
    >
      <Panel
        title={<span id="app-sandboxes-heading">Sandboxes</span>}
        action={
          sandboxes ? (
            <span className="whitespace-nowrap text-[11px] text-muted-foreground">
              {countLabel(running, "running", "running")} ·{" "}
              {countLabel(sandboxes.length, "recent", "recent")}
            </span>
          ) : null
        }
        className="h-full"
        contentClassName="p-0"
      >
        {error ? (
          <div className="flex min-h-32 items-center justify-center px-4 text-sm text-destructive">
            {error}
          </div>
        ) : pending ? (
          <RowsSkeleton rows={3} height="h-12" />
        ) : (sandboxes?.length ?? 0) === 0 ? (
          <PanelEmpty message="No sandboxes for this app" className="min-h-32" />
        ) : (
          <div className="divide-y divide-border/80">
            {sandboxes?.map((sandbox, index) => {
              return sandbox.container_id ? (
                <Link
                  key={sandbox.id}
                  to="/w/$workspace/sandboxes/$containerId"
                  params={{ workspace: workspaceName, containerId: sandbox.container_id }}
                  aria-label={`${sandbox.status === "running" ? "Open" : "View"} ${sandbox.name} sandbox${index > 0 ? ` ${index + 1}` : ""}`}
                  className="interactive-row grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1.5 px-4 py-3"
                >
                  <SandboxRowContent sandbox={sandbox} linked />
                </Link>
              ) : (
                <div
                  key={sandbox.id}
                  className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1.5 px-4 py-3"
                >
                  <SandboxRowContent sandbox={sandbox} linked={false} />
                </div>
              );
            })}
          </div>
        )}
      </Panel>
    </div>
  );
}

function SandboxRowContent({ sandbox, linked }: { sandbox: SandboxRow; linked: boolean }) {
  const startup =
    sandbox.time_to_started_ms == null ? "—" : formatDuration(sandbox.time_to_started_ms);
  const lifetime = sandbox.lifetime_ms == null ? "—" : formatDuration(sandbox.lifetime_ms);

  return (
    <>
      <span className="min-w-0">
        <span className="mono block truncate text-sm font-medium text-foreground">
          {sandbox.name}
        </span>
        <span className="mt-0.5 block text-[11px] text-muted-foreground">
          {sandbox.gpu.length > 0 ? sandbox.gpu.join(" → ") : "CPU"}
        </span>
      </span>
      <span className="flex items-center justify-end gap-2">
        <StatusChip status={sandbox.status} live={sandbox.status === "running"} />
        {linked ? (
          <ArrowUpRight
            className="interactive-row-indicator size-3.5 text-muted-foreground"
            aria-hidden="true"
          />
        ) : null}
      </span>
      <span className="col-span-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-muted-foreground">
        <span>Startup {startup}</span>
        <span aria-hidden="true">·</span>
        <span>Lifetime {lifetime}</span>
        <span aria-hidden="true">·</span>
        <span>
          Created{" "}
          <time dateTime={sandbox.created_at} title={exactTime(sandbox.created_at)}>
            {relativeTime(sandbox.created_at)}
          </time>
        </span>
      </span>
    </>
  );
}
