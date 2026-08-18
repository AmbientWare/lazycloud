import type { UsageCostBucket } from "@/lib/api/schemas";
import { calendarMonthWindow, type UsageCostWindow } from "@/lib/queries/usage";

export const usageRangeKeys = ["24h", "7d", "30d", "month"] as const;
export type UsageRangeKey = (typeof usageRangeKeys)[number];

export type UsageRange = {
  key: UsageRangeKey;
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
 * Both ends sit on a UTC boundary of the range's own interval, so the intervals
 * the server measures are the days and hours a customer reads them as. UTC
 * because that is the calendar the billing period is kept in; a local-midnight
 * window would put the customer's day boundary somewhere inside the platform's.
 *
 * The far end is the boundary closing the interval in progress, never the far
 * end of the calendar period: a month asked for whole answers with the days
 * nobody has lived yet, and somebody reading it on the third would see three
 * bars of spend and twenty-eight of nothing. Ending on a boundary rather than at
 * the instant of the call is also what makes the window cacheable — both ends
 * are part of the query key, and an end taken to the millisecond guarantees a
 * miss on every arrival at the page and leaves the abandoned window in the cache
 * until it is collected.
 */
export function usageRange(key: UsageRangeKey, at: Date): UsageRange {
  return { key, caption: RANGE_TEXT[key].caption, ...span(key, at) };
}

function span(key: UsageRangeKey, at: Date): { window: UsageCostWindow; bucket: UsageCostBucket } {
  const hour = startOfHour(at);
  const day = startOfDay(at);
  switch (key) {
    case "24h":
      return {
        window: { start: iso(hour - 23 * HOUR_MS), end: iso(hour + HOUR_MS) },
        bucket: "hour",
      };
    case "7d":
      return {
        window: { start: iso(day - 6 * DAY_MS), end: iso(day + DAY_MS) },
        bucket: "day",
      };
    case "30d":
      return {
        window: { start: iso(day - 29 * DAY_MS), end: iso(day + DAY_MS) },
        bucket: "day",
      };
    case "month":
      return {
        window: { start: calendarMonthWindow(at).start, end: iso(day + DAY_MS) },
        bucket: "day",
      };
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
