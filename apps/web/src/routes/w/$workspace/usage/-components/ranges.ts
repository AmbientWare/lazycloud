import type { UsageCostBucket } from "@/lib/api/schemas";
import { calendarMonthWindow, type UsageCostWindow } from "@/lib/queries/usage";

export const usageRangeKeys = ["24h", "7d", "30d", "month"] as const;
export type UsageRangeKey = (typeof usageRangeKeys)[number];

export type UsageRange = {
  key: UsageRangeKey;
  /** What the control is labelled with. */
  label: string;
  /** How the page's facts and its empty states name the span in a sentence. */
  caption: string;
  window: UsageCostWindow;
  bucket: UsageCostBucket;
};

const RANGE_TEXT: Record<UsageRangeKey, { label: string; caption: string }> = {
  "24h": { label: "24 hours", caption: "the last 24 hours" },
  "7d": { label: "7 days", caption: "the last 7 days" },
  "30d": { label: "30 days", caption: "the last 30 days" },
  month: { label: "This month", caption: "this month" },
};

export function usageRangeLabel(key: UsageRangeKey): string {
  return RANGE_TEXT[key].label;
}

const HOUR_MS = 3_600_000;
const DAY_MS = 24 * HOUR_MS;

/**
 * The window one range covers, and how finely it is read.
 *
 * Every window ends at `at` rather than at a future boundary. A month asked for
 * whole would answer with the days nobody has lived yet, and the chart drawn
 * from it is empty by construction — somebody reading it on the third would see
 * three bars of spend and twenty-eight of nothing.
 *
 * Every window opens on a UTC boundary of its own interval, so the intervals the
 * server measures from that start are the days and hours a customer reads them
 * as. UTC because that is the calendar the billing period is kept in; a
 * local-midnight window would put the customer's day boundary somewhere inside
 * the platform's.
 */
export function usageRange(key: UsageRangeKey, at: Date): UsageRange {
  return { key, ...RANGE_TEXT[key], ...span(key, at) };
}

function span(key: UsageRangeKey, at: Date): { window: UsageCostWindow; bucket: UsageCostBucket } {
  const end = at.toISOString();
  switch (key) {
    case "24h":
      return { window: { start: iso(startOfHour(at) - 23 * HOUR_MS), end }, bucket: "hour" };
    case "7d":
      return { window: { start: iso(startOfDay(at) - 6 * DAY_MS), end }, bucket: "day" };
    case "30d":
      return { window: { start: iso(startOfDay(at) - 29 * DAY_MS), end }, bucket: "day" };
    case "month":
      return { window: { start: calendarMonthWindow(at).start, end }, bucket: "day" };
  }
}

/** The label one interval carries on the axis, read in the calendar it was cut in. */
export function intervalLabel(startedAt: string, bucket: UsageCostBucket): string {
  const at = new Date(startedAt);
  if (Number.isNaN(at.getTime())) return startedAt;
  return (bucket === "hour" ? hourLabel : dayLabel).format(at);
}

const hourLabel = new Intl.DateTimeFormat(undefined, {
  timeZone: "UTC",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

const dayLabel = new Intl.DateTimeFormat(undefined, {
  timeZone: "UTC",
  day: "numeric",
  month: "short",
});

function startOfHour(at: Date): number {
  return Date.UTC(at.getUTCFullYear(), at.getUTCMonth(), at.getUTCDate(), at.getUTCHours());
}

function startOfDay(at: Date): number {
  return Date.UTC(at.getUTCFullYear(), at.getUTCMonth(), at.getUTCDate());
}

function iso(milliseconds: number): string {
  return new Date(milliseconds).toISOString();
}
