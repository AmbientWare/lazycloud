import { formatDistanceToNowStrict, parseISO } from "date-fns";

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

export type StatusTone = "success" | "warning" | "danger" | "muted";

/**
 * Status palette: green for healthy/complete, amber for pending/in-progress,
 * red for failures, neutral for inactive/cancelled and anything unknown.
 */
export function statusTone(value: RowValue): StatusTone {
  const normalized = String(value ?? "").toLowerCase();
  if (
    ["ok", "ready", "active", "deployed", "true", "running", "complete", "completed", "success", "healthy"].includes(
      normalized,
    )
  ) {
    return "success";
  }
  if (["pending", "queued", "starting", "retrying", "retry", "building", "warning"].includes(normalized)) {
    return "warning";
  }
  if (["failed", "error", "timeout", "expired", "unhealthy", "not ok"].includes(normalized)) {
    return "danger";
  }
  return "muted";
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
