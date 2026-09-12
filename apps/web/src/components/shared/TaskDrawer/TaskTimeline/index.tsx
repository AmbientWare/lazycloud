import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, type LinkProps } from "@tanstack/react-router";

import { useLiveNow } from "@/hooks/use-live-now";

import {
  axisTicks,
  flattenCallGraph,
  rowSegments,
  statusColor,
  timelineDomain,
  type TimelineRow,
} from "./timeline";
import { useWorkspaceLiveUpdates } from "@/lib/workspace-context";
import { isTerminalTaskStatus, type CallGraphNode, type Task } from "@/lib/api/schemas";
import { formatDuration } from "@/lib/format";
import { callGraphQueryOptions } from "@/lib/queries/tasks";
import { cn } from "@/lib/utils";
import { AxisLabels } from "./AxisLabels";

/** Parent and child tasks aligned on one elapsed-time axis. */
export function TaskTimeline({
  workspaceId,
  taskLink,
  task,
}: {
  workspaceId: string;
  /** Builds the drawer route for a timeline row, keeping page context. */
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
  const nowMs = useLiveNow(live);

  const { status: streamStatus } = useWorkspaceLiveUpdates();

  const domain = timelineDomain(rows, nowMs);
  if (!domain) {
    return <div className="p-3 text-sm text-muted-foreground">Not scheduled yet</div>;
  }
  const ticks = axisTicks(domain);

  return (
    <div className="min-w-0 overflow-hidden">
      <div className="min-w-0">
        <div className="flex min-h-9 flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-3 py-2 text-[11px] text-muted-foreground">
          <span>
            {rows.length === 1 ? "1 call" : `${rows.length} calls`} ·{" "}
            {formatDuration(domain.endMs - domain.startMs)} total
          </span>
          {live && streamStatus !== "open" ? (
            <span className="ml-3 flex items-center gap-1.5 text-warning" data-stream-stale="">
              <span className="size-1.5 rounded-full bg-warning" aria-hidden="true" />
              {streamStatus === "reconnecting" ? "Reconnecting" : "Connecting"}
            </span>
          ) : null}
          <span className="ml-auto flex flex-wrap items-center gap-3" aria-label="Timeline legend">
            <span className="flex items-center gap-1.5">
              <span className="h-1.5 w-3 bg-muted-foreground/40" aria-hidden="true" />
              Queued
            </span>
            <span className="flex items-center gap-1.5">
              <span className="h-1.5 w-3 bg-brand" aria-hidden="true" />
              Execution
            </span>
          </span>
        </div>

        <div className="grid min-w-0 grid-cols-[minmax(8rem,12rem)_minmax(8rem,1fr)] px-3 pb-3 text-xs">
          <div className="flex h-8 items-end border-b border-border/60 pb-1 text-[10px] text-muted-foreground">
            Task
          </div>
          <div className="relative h-8 border-b border-border/60" aria-label="Elapsed time axis">
            <AxisLabels ticks={ticks} className="bottom-1" />
          </div>

          {rows.map((row) => (
            <TimelineBarRow
              key={row.node.task_id}
              row={row}
              highlighted={row.node.task_id === task.id}
              taskLink={taskLink}
              domain={domain}
              ticks={ticks}
              nowMs={nowMs}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function TimelineBarRow({
  row,
  highlighted,
  taskLink,
  domain,
  ticks,
  nowMs,
}: {
  row: TimelineRow;
  highlighted: boolean;
  /** Builds the drawer route for a timeline row, keeping page context. */
  taskLink: (taskId: string) => Pick<LinkProps, "to" | "params" | "search">;
  domain: NonNullable<ReturnType<typeof timelineDomain>>;
  ticks: ReturnType<typeof axisTicks>;
  nowMs: number;
}) {
  const { node, depth, ancestorContinues, isLastSibling } = row;
  const segments = rowSegments(node, domain, nowMs);
  const runSegment = segments.find((item) => item.kind === "run");
  const queuedSegment = segments.find((item) => item.kind === "queued");
  const sourceLabel = node.function_name || node.name || "Task";
  const label = taskLabel(sourceLabel);
  const elapsed = runSegment
    ? formatDuration(runSegment.durationMs)
    : queuedSegment
      ? `${formatDuration(queuedSegment.durationMs)} queued`
      : "Not started";
  const status = statusLabel(node.status);

  return (
    <Link
      {...taskLink(node.task_id)}
      aria-current={highlighted ? "page" : undefined}
      aria-label={`${label}, ${status}, ${elapsed}`}
      data-selected={highlighted}
      className={cn(
        "interactive-row group col-span-2 grid min-w-0 grid-cols-subgrid border-b border-border/50",
      )}
      title={`${sourceLabel} · ${status} · ${elapsed}`}
    >
      <span className="flex h-12 min-w-0 items-center pr-3">
        <TreeBranch
          depth={depth}
          ancestorContinues={ancestorContinues}
          isLastSibling={isLastSibling}
        />
        <span className="flex min-w-0 flex-1 flex-col justify-center">
          <span
            className={cn(
              "mono truncate text-[11px]",
              highlighted ? "font-medium text-foreground" : "text-foreground/90",
            )}
          >
            {label}
          </span>
          <span className="flex min-w-0 items-center gap-1.5 text-[10px] text-muted-foreground">
            <span
              className={cn(
                "size-1.5 shrink-0 rounded-full",
                node.status === "running" && "pulse-live",
              )}
              style={{ background: statusColor(node.status) }}
              aria-hidden="true"
            />
            <span className="truncate">{status}</span>
            <span aria-hidden="true">·</span>
            <span className="shrink-0 tabular-nums">{elapsed}</span>
          </span>
        </span>
      </span>

      <span className="relative h-12 min-w-0 overflow-hidden">
        {ticks.map((tick) => (
          <span
            key={tick.timestampMs}
            className="absolute inset-y-0 border-l border-border/40"
            style={{ left: `${tick.leftPct}%` }}
            aria-hidden="true"
          />
        ))}
        {segments.map((segment) => (
          <span
            key={segment.kind}
            className={cn(
              "absolute top-[18px] h-3",
              segment.kind === "run" ? "bg-brand" : "bg-muted-foreground/40",
            )}
            style={{
              left: `${segment.leftPct}%`,
              width: `${segment.widthPct}%`,
              minWidth: segment.durationMs > 0 ? "2px" : undefined,
            }}
            title={`${segment.kind === "run" ? "Execution" : "Queued"}: ${formatDuration(segment.durationMs)}`}
            aria-label={`${segment.kind === "run" ? "Execution" : "Queued"} ${formatDuration(segment.durationMs)}`}
          >
            {segment.kind === "run" && isTerminalTaskStatus(node.status) ? (
              <span
                className="absolute -right-px -top-0.5 h-4 w-0.5"
                style={{ background: statusColor(node.status) }}
                aria-hidden="true"
              />
            ) : null}
          </span>
        ))}
      </span>
    </Link>
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
