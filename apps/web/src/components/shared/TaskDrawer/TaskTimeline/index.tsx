import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, type LinkProps } from "@tanstack/react-router";

import { useLiveNow } from "@/hooks/use-live-now";
import { ContentTransition } from "@/components/shared/ContentTransition";
import { PanelError } from "@/components/shared/PanelError";
import { Skeleton } from "@/components/ui/skeleton";
import { flattenCallGraph, statusColor, timelineDomain, type TimelineRow } from "./timeline";
import { LifecycleStrip } from "./LifecycleStrip";
import { executionPhases } from "./phases";
import { useWorkspaceLiveUpdates } from "@/lib/workspace-context";
import {
  isTerminalTaskStatus,
  type CallGraphNode,
  type ContainerLifecycleMetric,
  type Task,
} from "@/lib/api/schemas";
import { callGraphLifecycleQueryOptions } from "@/lib/queries/events";
import { formatDuration } from "@/lib/format";
import { callGraphQueryOptions } from "@/lib/queries/tasks";
import { cn } from "@/lib/utils";

/** Parent and child tasks aligned on one elapsed-time axis. */
export function TaskTimeline({
  workspaceId,
  taskLink,
  task,
}: {
  workspaceId: string;
  taskLink: (taskId: string) => Pick<LinkProps, "to" | "params" | "search">;
  task: Task;
}) {
  const rootId = task.root_task_id || task.id;
  const graph = useQuery(callGraphQueryOptions(workspaceId, rootId));
  const rows = useMemo<TimelineRow[]>(() => {
    const flattened = flattenCallGraph(graph.data?.nodes ?? []);
    return flattened.length > 0
      ? flattened
      : [{ node: taskAsNode(task), depth: 0, ancestorContinues: [], isLastSibling: true }];
  }, [graph.data, task]);
  const live = rows.some((row) => !isTerminalTaskStatus(row.node.status));
  const containerIds = rows.flatMap(({ node }) => (node.container_id ? [node.container_id] : []));
  const summaries = useQuery({
    ...callGraphLifecycleQueryOptions(workspaceId, rootId, containerIds, live),
    enabled: !graph.isPending && containerIds.length > 0,
  });
  const lifecycleByContainer = new Map(
    summaries.data?.items.map((item) => [item.container_id, item.lifecycle]),
  );
  const nowMs = useLiveNow(live);
  const { status: streamStatus } = useWorkspaceLiveUpdates();
  const domain = timelineDomain(rows, nowMs);
  if (graph.isPending || (containerIds.length > 0 && summaries.isPending)) {
    return (
      <ContentTransition pending className="space-y-3 p-4">
        <Skeleton className="h-4 w-24" />
        <Skeleton className="h-8 w-full" />
        <Skeleton className="h-4 w-36" />
      </ContentTransition>
    );
  }
  if (graph.isError && !graph.data) return <PanelError message={graph.error.message} />;
  if (summaries.isError && !summaries.data) return <PanelError message={summaries.error.message} />;
  if (!domain) {
    return <div className="p-3 text-sm text-muted-foreground">Not scheduled yet</div>;
  }

  return (
    <ContentTransition className="min-w-0">
      <div className="flex min-h-9 flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-4 py-2 text-[11px] text-muted-foreground">
        <span>
          {rows.length === 1 ? "1 call" : `${rows.length} calls`} ·{" "}
          {formatDuration(domain.endMs - domain.startMs)} total
        </span>
        {live && streamStatus !== "open" ? (
          <span className="text-warning" data-stream-stale="">
            {streamStatus === "reconnecting" ? "Reconnecting" : "Connecting"}
          </span>
        ) : null}
      </div>
      <div className="divide-y divide-border/60">
        {rows.map((row) => (
          <TimelineBarRow
            key={row.node.task_id}
            row={row}
            highlighted={row.node.task_id === task.id}
            taskLink={taskLink}
            domain={domain}
            nowMs={nowMs}
            lifecycle={lifecycleByContainer.get(row.node.container_id ?? "") ?? []}
          />
        ))}
      </div>
    </ContentTransition>
  );
}

function TimelineBarRow({
  row,
  highlighted,
  taskLink,
  domain,
  nowMs,
  lifecycle,
}: {
  row: TimelineRow;
  highlighted: boolean;
  taskLink: (taskId: string) => Pick<LinkProps, "to" | "params" | "search">;
  domain: NonNullable<ReturnType<typeof timelineDomain>>;
  nowMs: number;
  lifecycle: ContainerLifecycleMetric[];
}) {
  const { node, depth, ancestorContinues, isLastSibling } = row;
  const phases = executionPhases(node, lifecycle, nowMs, domain);
  const sourceLabel = node.function_name || node.name || "Task";
  const label = taskLabel(sourceLabel);
  const status = statusLabel(node.status);

  return (
    <div className={cn("min-w-0 px-4 py-2.5", highlighted && "bg-muted/25")}>
      <div className="flex h-6 min-w-0 items-center gap-2">
        <TreeBranch
          depth={depth}
          ancestorContinues={ancestorContinues}
          isLastSibling={isLastSibling}
        />
        <Link
          {...taskLink(node.task_id)}
          aria-current={highlighted ? "page" : undefined}
          className="interactive-link mono min-w-0 truncate text-xs font-medium"
          title={sourceLabel}
        >
          {label}
        </Link>
        <span className="ml-auto shrink-0 text-[11px]" style={{ color: statusColor(node.status) }}>
          {status}
        </span>
      </div>
      <LifecycleStrip phases={phases} domain={domain} showLegend={false} />
    </div>
  );
}

function TreeBranch({
  depth,
  ancestorContinues,
  isLastSibling,
}: {
  depth: number;
  ancestorContinues: boolean[];
  isLastSibling: boolean;
}) {
  if (depth === 0) return <span className="w-1 shrink-0" aria-hidden="true" />;
  const hiddenDepth = Math.max(ancestorContinues.length - 3, 0);
  const visibleAncestors = ancestorContinues.slice(hiddenDepth);
  return (
    <span className="flex h-full shrink-0" aria-hidden="true">
      {hiddenDepth > 0 ? (
        <span className="flex w-3 shrink-0 items-center justify-center text-[9px] text-muted-foreground">
          …
        </span>
      ) : null}
      {visibleAncestors.map((continues, index) => (
        <span key={index} className="relative w-3 shrink-0">
          {continues ? (
            <span className="absolute inset-y-0 left-1.5 border-l border-border" />
          ) : null}
        </span>
      ))}
      <span className="relative w-3 shrink-0">
        <span
          className={cn(
            "absolute left-1.5 top-0 border-l border-border",
            isLastSibling ? "h-1/2" : "h-full",
          )}
        />
        <span className="absolute left-1.5 top-1/2 w-1.5 border-t border-border" />
      </span>
    </span>
  );
}

function statusLabel(status: string): string {
  if (!status) return "Unknown";
  return status.charAt(0).toUpperCase() + status.slice(1).replaceAll("_", " ");
}

function taskLabel(value: string): string {
  const handler = value.includes(":") ? (value.split(":").at(-1) ?? value) : value;
  return handler.replace(/^function[-_:]/, "") || "Task";
}

function taskAsNode(task: Task): CallGraphNode {
  return {
    task_id: task.id,
    container_id: task.container_id ?? null,
    parent_task_id: task.parent_task_id ?? "",
    root_task_id: task.root_task_id ?? task.id,
    status: task.status,
    name: task.name,
    function_name: "",
    created_at: task.created_at,
    started_at: task.started_at ?? null,
    finished_at: task.finished_at ?? null,
    dependencies: [],
    children: [],
  };
}
