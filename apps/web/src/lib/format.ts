import { formatDistanceToNowStrict, parseISO } from "date-fns";
import {
  resourceLimit,
  resourceRequest,
  type CpuRequest,
  type MemoryRequest,
} from "@/lib/api/schemas/resources";

import { isKnownTaskStatus, isTerminalTaskStatus } from "@/lib/api/schemas/tasks";

import type { RowValue } from "@/lib/api/resources";

export function displayValue(value: RowValue): string {
  if (value === null || value === undefined || value === "") return "None";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return Intl.NumberFormat().format(value);
  if (isTimestamp(value)) return relativeTime(value);
  return value;
}

export function relativeTime(value: string | undefined): string {
  if (!value) return "None";
  try {
    return `${formatDistanceToNowStrict(parseISO(value), { addSuffix: true })}`;
  } catch {
    return value;
  }
}

/** The machine-precise reading of a timestamp, for the `title` behind a relative one. */
export function exactTime(value: string): string {
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? value : timestamp.toLocaleString();
}

/** `12 apps`, `1 app` — the count first, because that is what is being scanned. */
export function countLabel(value: number, singular: string, plural = `${singular}s`): string {
  return `${value.toLocaleString()} ${value === 1 ? singular : plural}`;
}

/** A hyphenated wire token — a workload kind, a status — as the words a reader sees. */
export function formatKind(kind: string): string {
  return kind
    .split("-")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

/**
 * A fraction of a whole, in words.
 *
 * One rule everywhere a share is printed, because the same figure appearing as
 * `<1%` beside one bar and `0.3%` beside another reads as two different
 * measurements. Sub-percent shares keep a digit rather than collapsing to `0%`:
 * a row that cost something, or a failure rate over a busy window, is a reading
 * the reader acts on, and rounding it to nothing states the opposite.
 */
export function shareLabel(share: number): string {
  if (!Number.isFinite(share) || share <= 0) return "0%";
  const percent = share * 100;
  if (percent < 0.1) return "<0.1%";
  if (percent < 10) return `${percent.toFixed(1)}%`;
  return `${Math.round(percent)}%`;
}

export type StatusTone = "success" | "warning" | "danger" | "muted";

/**
 * Status palette: green for healthy/complete, amber for pending/in-progress,
 * red for failures, neutral for inactive/cancelled and anything unknown.
 */
export function statusTone(value: RowValue): StatusTone {
  const normalized = String(value ?? "").toLowerCase();
  if (
    [
      "ok",
      "ready",
      "active",
      "deployed",
      "true",
      "running",
      "complete",
      "completed",
      "success",
      "healthy",
    ].includes(normalized)
  ) {
    return "success";
  }
  if (
    ["pending", "queued", "starting", "retrying", "retry", "building", "warning"].includes(
      normalized,
    )
  ) {
    return "warning";
  }
  if (["failed", "error", "timeout", "expired", "unhealthy", "not ok"].includes(normalized)) {
    return "danger";
  }
  return "muted";
}

export type TaskActivityBand = "succeeded" | "inFlight" | "failed" | "other";

/**
 * Which band of an activity bar a task status belongs to.
 *
 * Parts company with `statusTone` on `running`, which a chip paints healthy
 * green because a running task is a working one. Over a window of finished
 * work it is not a task that succeeded, and counting it as one is what lets an
 * app whose queue never drained read as an app where everything worked. A
 * status this build does not know lands in `other` for the same reason.
 */
export function taskActivityBand(status: string): TaskActivityBand {
  const normalized = status.toLowerCase();
  if (!isKnownTaskStatus(normalized)) return "other";
  if (!isTerminalTaskStatus(normalized)) return "inFlight";
  if (statusTone(normalized) === "danger") return "failed";
  return normalized === "complete" ? "succeeded" : "other";
}

export function formatDuration(milliseconds: number): string {
  if (milliseconds < 1_000) return `${Math.round(milliseconds)}ms`;
  const seconds = milliseconds / 1_000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  if (minutes < 60) return `${minutes}m ${remainder}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

export function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size < 0) return "None";
  if (size < 1024) return `${Math.round(size)} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"] as const;
  let value = size / 1024;
  let unit: (typeof units)[number] = units[0];
  for (const candidate of units.slice(1)) {
    if (value < 1024) break;
    value /= 1024;
    unit = candidate;
  }
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${unit}`;
}

export function durationBetween(
  startedAt: string | null | undefined,
  finishedAt: string | null | undefined,
): string | null {
  if (!startedAt) return null;
  const end = finishedAt ? Date.parse(finishedAt) : Date.now();
  const start = Date.parse(startedAt);
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return null;
  return formatDuration(end - start);
}

/**
 * Startup latency: creation to execution start. Unlike `durationBetween`,
 * this stays empty until the task has actually started.
 */
export function startupBetween(
  createdAt: string | null | undefined,
  startedAt: string | null | undefined,
): string | null {
  if (!createdAt || !startedAt) return null;
  const created = Date.parse(createdAt);
  const started = Date.parse(startedAt);
  if (Number.isNaN(created) || Number.isNaN(started) || started < created) return null;
  return formatDuration(started - created);
}

function isTimestamp(value: string): boolean {
  return /^\d{4}-\d{2}-\d{2}T/.test(value);
}

/**
 * Why a container stopped, in the words its owner needs.
 *
 * Mirrors `StopContainerReason.describe` in `shared/container_requests.py`, and
 * changes with it. UNKNOWN is absent on purpose: it is the column default, so
 * it also means the reason has not arrived, and a container still running
 * would otherwise be handed a cause.
 */
const STOP_REASONS: Record<string, string | undefined> = {
  TTL: "It reached its time limit",
  USER: "It was stopped from this account",
  SCHEDULER: "The platform moved the work",
  PREEMPTED: "Its machine was reclaimed",
  ADMIN: "The platform stopped it",
  UNFUNDED: "The account has no payment method on file",
  MEMORY_EVICTED:
    "The machine ran out of memory and this container was using the most above its request",
};

/**
 * The label for a terminal container's stop reason, or null when there is
 * nothing truthful to say — still running, or a reason this build does not
 * know, which renders nothing rather than a raw wire value.
 */
export function stopReasonLabel(
  terminationReason: string | undefined,
  status: string,
): string | null {
  if (status === "running" || status === "pending") return null;
  return (terminationReason && STOP_REASONS[terminationReason]) || null;
}

/**
 * A resource a workload stated, with its ceiling when the author named one.
 *
 * The pair form is what a container may grow into, so hiding the second figure
 * would show a workload as smaller than it is allowed to become.
 */
export function resourceAllocation(
  value: CpuRequest | MemoryRequest | null | undefined,
  unit = "",
): string {
  const request = resourceRequest(value);
  if (request == null) return "Default";
  const suffix = unit ? ` ${unit}` : "";
  const limit = resourceLimit(value);
  if (limit == null) return `${request}${suffix}`;
  return `${request}${suffix} (limit ${limit}${suffix})`;
}
