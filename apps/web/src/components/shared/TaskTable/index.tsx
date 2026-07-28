import type { ReactNode } from "react";
import { Link, type LinkProps } from "@tanstack/react-router";

import { StatusChip } from "@/components/shared/StatusChip";
import { StubKindIcon } from "@/components/shared/StubKindIcon";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { Task } from "@/lib/api/schemas";
import { durationBetween, relativeTime, startupBetween } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useWorkspace } from "@/lib/workspace-context";

export function TaskTable({
  tasks,
  taskLink,
  showApp = true,
  emptyMessage = "No tasks",
  className,
  continuation,
}: {
  tasks: Task[] | undefined;
  /** Builds the drawer route for a task; keeps the drawer nested in the page context. */
  taskLink: (taskId: string) => Pick<LinkProps, "to" | "params" | "search">;
  showApp?: boolean;
  emptyMessage?: string;
  className?: string;
  continuation?: ReactNode;
}) {
  const { workspace } = useWorkspace();
  const rows = tasks ?? [];

  if (!tasks) {
    return (
      <div className={cn("space-y-2 p-4", className)} aria-hidden="true">
        {Array.from({ length: 5 }, (_, index) => (
          <Skeleton key={index} className="h-6 w-full" />
        ))}
      </div>
    );
  }

  if (rows.length === 0) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
        {emptyMessage}
      </div>
    );
  }

  return (
    <div data-task-table-scroll="" className={cn("overflow-auto", className)}>
      <Table className="min-w-[820px]">
        <TableHeader className="sticky top-0 z-10 bg-card">
          <TableRow className="border-b border-border hover:bg-transparent">
            <TableHead>Task</TableHead>
            <TableHead>Workload</TableHead>
            {showApp ? <TableHead>App</TableHead> : null}
            <TableHead>Status</TableHead>
            <TableHead>Requested</TableHead>
            <TableHead>Started</TableHead>
            <TableHead>Startup</TableHead>
            <TableHead>Duration</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((task) => {
            const kind = task.workload?.kind;
            return (
              <TableRow key={task.id}>
                <TableCell className="max-w-[280px]">
                  <Link
                    {...taskLink(task.id)}
                    className="interactive-link flex min-w-0 items-center gap-2 font-medium text-foreground"
                  >
                    <span className="truncate" title={task.id}>
                      {task.name}
                    </span>
                  </Link>
                </TableCell>
                <TableCell className="max-w-[220px]">
                  {task.workload ? (
                    <span className="flex min-w-0 items-center gap-2">
                      <StubKindIcon kind={kind ?? "function"} className="size-3 shrink-0" />
                      <Link
                        to="/w/$workspace/apps/$appId/workloads/$name"
                        params={{
                          workspace: workspace.name,
                          appId: task.app_id ?? "",
                          name: task.workload.name,
                        }}
                        disabled={!task.app_id}
                        className="interactive-link min-w-0 truncate text-xs text-foreground disabled:pointer-events-none"
                      >
                        {task.workload.name}
                      </Link>
                      <span className="shrink-0 text-[11px] text-muted-foreground">
                        {kind}
                        {task.deployment ? ` · v${task.deployment.version}` : ""}
                      </span>
                    </span>
                  ) : (
                    <span className="text-xs text-muted-foreground">—</span>
                  )}
                </TableCell>
                {showApp ? (
                  <TableCell className="max-w-[160px] truncate text-xs text-muted-foreground">
                    {task.app && task.app_id ? (
                      <Link
                        to="/w/$workspace/apps/$appId"
                        params={{ workspace: workspace.name, appId: task.app_id }}
                        className="interactive-link"
                      >
                        {task.app.name}
                      </Link>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                ) : null}
                <TableCell>
                  <StatusChip status={task.status} live={task.status === "running"} />
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  <time dateTime={task.created_at} title={exactTime(task.created_at)}>
                    {relativeTime(task.created_at)}
                  </time>
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {task.started_at ? (
                    <time dateTime={task.started_at} title={exactTime(task.started_at)}>
                      {relativeTime(task.started_at)}
                    </time>
                  ) : (
                    "—"
                  )}
                </TableCell>
                <TableCell className="text-xs tabular-nums text-muted-foreground">
                  {startupBetween(task.created_at, task.started_at) ?? "—"}
                </TableCell>
                <TableCell className="text-xs tabular-nums text-muted-foreground">
                  {durationBetween(task.started_at, task.finished_at) ?? "—"}
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
      {continuation}
    </div>
  );
}

function exactTime(value: string): string {
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? value : timestamp.toLocaleString();
}
