import { formatDistanceStrict, parseISO } from "date-fns";

export type RowValue = string | number | boolean | null | undefined;

export function displayValue(value: RowValue): string {
  if (value === null || value === undefined || value === "") return "None";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return Intl.NumberFormat().format(value);
  if (isTimestamp(value)) return relativeTime(value);
  return value;
}

export function relativeTime(value: string | undefined, now = Date.now()): string {
  if (!value) return "None";
  try {
    return formatDistanceStrict(parseISO(value), now, { addSuffix: true });
  } catch {
    return value;
  }
}

/** The machine-precise reading of a timestamp, for the `title` behind a relative one. */
export function exactTime(value: string): string {
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? value : timestamp.toLocaleString();
}

/** The end of an ID. IDs are UUIDv7, whose first digits are a timestamp that IDs made close together share. */
export function shortId(id: string): string {
  return id.slice(-8);
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

export type TaskActivityBand = "succeeded" | "inFlight" | "failed" | "other";

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
  now = Date.now(),
): string | null {
  if (!startedAt) return null;
  const end = finishedAt ? Date.parse(finishedAt) : now;
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

/**
 * A resource a workload states as a reservation, or as a `[reserve, limit]` pair.
 *
 */
export type CpuRequest = number | [number, number];
export type MemoryRequest = string | number | [string | number, string | number];

/** The reservation half, which is what capacity is sized against. */
export function resourceRequest(
  value: CpuRequest | MemoryRequest | null | undefined,
): string | number | null {
  if (value == null) return null;
  return Array.isArray(value) ? value[0] : value;
}

/** The ceiling half, present only when its author named one. */
export function resourceLimit(
  value: CpuRequest | MemoryRequest | null | undefined,
): string | number | null {
  return Array.isArray(value) ? value[1] : null;
}
