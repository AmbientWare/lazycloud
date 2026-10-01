import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { ChevronRight } from "lucide-react";

import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { Panel } from "@/components/shared/Panel";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { RowsSkeleton } from "@/components/shared/RowsSkeleton";
import { StatusChip } from "@/components/shared/StatusChip";
import { StubKindIcon } from "@/components/shared/StubKindIcon";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { Schemas } from "@/lib/api/client";
import { deployedAt, workloadRunning } from "@/lib/queries/deployments";
import { formatKind } from "@/lib/format";

import { WorkloadRowActions } from "./WorkloadRowActions";

export function AppWorkloadsSection({
  workspace,
  app,
  workloads,
  pending,
  error,
}: {
  workspace: string;
  app: string;
  /** Every deployed workload of the app, newest deploy first. */
  workloads: Schemas["DeployedWorkload"][] | undefined;
  pending: boolean;
  error: string | undefined;
}) {
  const [kind, setKind] = useState<string>();
  const all = workloads ?? [];
  // The kinds this app actually deploys, not the kinds one could. A filter
  // offering a kind nothing here has is an option whose only outcome is an
  // empty list.
  const kindsPresent = [...new Set(all.map((workload) => workload.kind))].sort();
  const rows = kind ? all.filter((workload) => workload.kind === kind) : all;

  return (
    <div
      role="region"
      aria-labelledby="app-workloads-heading"
      className="min-h-[26rem] lg:h-full lg:min-h-0"
    >
      <Panel
        pending={pending}
        title={<span id="app-workloads-heading">Workloads</span>}
        action={
          <Select
            value={kind ?? "all"}
            onValueChange={(next) => setKind(next === "all" ? undefined : next)}
          >
            <SelectTrigger aria-label="Type" size="sm" className="w-40 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent align="end">
              <SelectItem value="all">All types</SelectItem>
              {kindsPresent.map((option) => (
                <SelectItem key={option} value={option}>
                  {formatKind(option)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        }
        className="h-full"
        contentClassName="p-0"
      >
        {pending ? (
          <RowsSkeleton rows={4} height="h-14" />
        ) : error ? (
          <div className="flex min-h-48 items-center justify-center px-4 text-sm text-destructive">
            {error}
          </div>
        ) : all.length === 0 ? (
          <PanelEmpty message="No deployed workloads for this app" className="min-h-48" />
        ) : rows.length === 0 ? (
          <div>
            <PanelEmpty message="No workloads match this type" className="min-h-48" />
          </div>
        ) : (
          <div>
            <div
              className="sticky top-0 z-10 hidden grid-cols-[minmax(8rem,1fr)_4.5rem_6rem_5.75rem_6.25rem_1rem] gap-2 border-b border-border bg-card px-3 py-2 text-[10px] text-muted-foreground xl:grid"
              aria-hidden="true"
            >
              <span>Workload</span>
              <span>Version</span>
              <span>Containers</span>
              <span>Status</span>
              <span>Deployed</span>
              <span />
            </div>
            <div className="divide-y divide-border/80">
              {rows.map((workload) => {
                const running = workload.running_containers;
                const active = workloadRunning(workload);
                return (
                  <Link
                    key={workload.id}
                    to="/w/$workspace/apps/$app/workloads/$kind/$name"
                    params={{ workspace, app, kind: workload.kind, name: workload.name }}
                    className="interactive-row group grid min-w-0 gap-x-2 gap-y-2 px-3 py-3 xl:grid-cols-[minmax(8rem,1fr)_4.5rem_6rem_5.75rem_6.25rem_1rem] xl:items-center"
                  >
                    <span className="flex min-w-0 items-center gap-2.5">
                      <StubKindIcon kind={workload.kind} className="size-3.5" />
                      <span className="min-w-0">
                        <span className="mono block truncate text-sm font-medium text-foreground">
                          {workload.name}
                        </span>
                        <span className="block text-[11px] text-muted-foreground">
                          {formatKind(workload.kind)}
                        </span>
                      </span>
                    </span>

                    <span className="hidden text-xs xl:block">
                      <span className="mono block text-foreground">v{workload.version}</span>
                    </span>
                    <span className="hidden text-xs xl:block">
                      <span className="mono block text-foreground">{running} running</span>
                    </span>
                    <span className="hidden xl:block">
                      <StatusChip status={active ? "deployed" : "inactive"} live={active} />
                    </span>
                    <LiveRelativeTime
                      value={deployedAt(workload)}
                      className="hidden text-xs text-muted-foreground xl:block"
                    />

                    <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground xl:hidden">
                      <span className="mono text-foreground">v{workload.version}</span>
                      <span aria-hidden="true">·</span>
                      <span>{running} running</span>
                      <span aria-hidden="true">·</span>
                      <LiveRelativeTime value={deployedAt(workload)} />
                      <StatusChip status={active ? "deployed" : "inactive"} live={active} />
                    </span>
                    <span className="flex items-center justify-end gap-1">
                      <WorkloadRowActions workload={workload} workspace={workspace} />
                      <ChevronRight
                        className="interactive-row-indicator hidden size-3.5 text-muted-foreground transition-colors xl:block"
                        aria-hidden="true"
                      />
                    </span>
                  </Link>
                );
              })}
            </div>
          </div>
        )}
      </Panel>
    </div>
  );
}
