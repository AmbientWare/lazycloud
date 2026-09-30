import type { CallGraphNode } from "@/lib/api/schemas";

export type TimelineRow = {
  node: CallGraphNode;
  depth: number;
  /** Vertical tree lines that continue through this row at each ancestor depth. */
  ancestorContinues: boolean[];
  isLastSibling: boolean;
};

export type TimeDomain = {
  startMs: number;
  endMs: number;
};

export type AxisTick = {
  leftPct: number;
  label: string;
  timestampMs: number;
};

const TICK_INTERVALS_MS = [
  1_000,
  2_000,
  5_000,
  10_000,
  15_000,
  30_000,
  60_000,
  2 * 60_000,
  5 * 60_000,
  10 * 60_000,
  15 * 60_000,
  30 * 60_000,
  60 * 60_000,
  2 * 60 * 60_000,
  6 * 60 * 60_000,
  12 * 60 * 60_000,
  24 * 60 * 60_000,
] as const;

/** Depth-first flatten of the call graph, preserving server child order and branch shape. */
export function flattenCallGraph(nodes: CallGraphNode[]): TimelineRow[] {
  const rows: TimelineRow[] = [];
  const visit = (
    node: CallGraphNode,
    depth: number,
    ancestorContinues: boolean[],
    isLastSibling: boolean,
  ) => {
    rows.push({ node, depth, ancestorContinues, isLastSibling });
    node.children.forEach((child, index) => {
      visit(
        child,
        depth + 1,
        depth === 0 ? [] : [...ancestorContinues, !isLastSibling],
        index === node.children.length - 1,
      );
    });
  };
  nodes.forEach((node, index) => visit(node, 0, [], index === nodes.length - 1));
  return rows;
}

/**
 * Shared time window covering every row: earliest creation to the latest
 * finish, extended to `nowMs` while any task is still unfinished.
 */
export function timelineDomain(rows: TimelineRow[], nowMs: number): TimeDomain | null {
  let startMs = Number.POSITIVE_INFINITY;
  let endMs = Number.NEGATIVE_INFINITY;
  for (const { node } of rows) {
    const created = parseMs(node.created_at);
    if (created === null) continue;
    startMs = Math.min(startMs, created);
    const finished = parseMs(node.finished_at);
    endMs = Math.max(endMs, finished ?? nowMs);
  }
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) return null;
  if (endMs - startMs < 1_000) endMs = startMs + 1_000;
  return { startMs, endMs };
}

/** Elapsed-time ticks using operationally useful 1/2/5-style intervals. */
export function axisTicks(domain: TimeDomain, targetIntervals = 4): AxisTick[] {
  const spanMs = domain.endMs - domain.startMs;
  const desiredInterval = spanMs / Math.max(targetIntervals, 1);
  const intervalMs =
    TICK_INTERVALS_MS.find((candidate) => candidate >= desiredInterval) ??
    Math.ceil(desiredInterval / (24 * 60 * 60_000)) * 24 * 60 * 60_000;
  const offsets = [0];
  for (let offset = intervalMs; offset < spanMs; offset += intervalMs) offsets.push(offset);
  if (spanMs > 0) {
    const previousOffset = offsets.at(-1) ?? 0;
    const terminalGapPct = ((spanMs - previousOffset) / spanMs) * 100;
    if (offsets.length > 1 && terminalGapPct < 8) offsets.pop();
    offsets.push(spanMs);
  }

  return offsets.map((offset) => ({
    leftPct: (offset / spanMs) * 100,
    label: elapsedLabel(offset),
    timestampMs: domain.startMs + offset,
  }));
}

export function elapsedLabel(milliseconds: number): string {
  const seconds = Math.max(milliseconds, 0) / 1_000;
  if (seconds < 60) return `${formatElapsedNumber(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    const remainder = Math.round(seconds % 60);
    return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
  }
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
}

export function statusColor(status: string): string {
  switch (status) {
    case "complete":
      return "var(--positive)";
    case "running":
      return "var(--positive)";
    case "retry":
    case "pending":
      return "var(--warning)";
    case "failed":
    case "timeout":
    case "expired":
      return "var(--destructive)";
    case "cancelled":
      return "var(--muted-foreground)";
    default:
      return "var(--chart-5)";
  }
}

function parseMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms;
}

function formatElapsedNumber(value: number): string {
  if (value === 0) return "0";
  if (value < 10) return value.toFixed(1).replace(/\.0$/, "");
  return String(Math.round(value));
}
