import type { TaskTimeWindowBucket } from "@/lib/api/schemas";
import { taskActivityBand, type TaskActivityBand } from "@/lib/format";

const HOUR_MS = 60 * 60 * 1_000;
const HOUR_COUNT = 24;

export type AppRunActivity = {
  tasks: number[];
  /** Per-hour counts per band, every status the server reported placed in one. */
  bands: Record<TaskActivityBand, number[]>;
  totals: Record<TaskActivityBand, number>;
  total: number;
};

/** Maps sparse server-owned hourly buckets onto the current 24 UTC hours. */
export function appRunActivity(
  buckets: TaskTimeWindowBucket[] | undefined,
  now = new Date(),
): AppRunActivity {
  const tasks = Array.from({ length: HOUR_COUNT }, () => 0);
  const bands = emptyBands();
  const currentHour = Math.floor(now.getTime() / HOUR_MS) * HOUR_MS;
  const firstHour = currentHour - (HOUR_COUNT - 1) * HOUR_MS;

  for (const bucket of buckets ?? []) {
    const timestamp = new Date(bucket.timestamp).getTime();
    if (!Number.isFinite(timestamp)) continue;
    const hour = Math.floor(timestamp / HOUR_MS) * HOUR_MS;
    const index = Math.floor((hour - firstHour) / HOUR_MS);
    if (index < 0 || index >= HOUR_COUNT) continue;

    tasks[index] += Math.max(Math.trunc(bucket.count), 0);
    for (const [status, value] of Object.entries(bucket.status_counts)) {
      const amount = Math.max(Math.trunc(value), 0);
      if (amount === 0) continue;
      bands[taskActivityBand(status)][index] += amount;
    }
  }

  return summarizeActivity(tasks, bands);
}

/** The same 24 hours from the app list, which sends one series per band.

    The list cannot afford a bucket per app per status, so it sends four totals
    an hour instead. Both sources end up in this shape so one chart draws them,
    and the remainder of an hour that no band claims stays `other` rather than
    being folded into the nearest one. */
export function appRunActivityFromSeries(series: {
  activity: number[];
  failures: number[];
  pending: number[];
  succeeded: number[];
}): AppRunActivity {
  const tasks = alignToWindow(series.activity);
  const bands = {
    failed: alignToWindow(series.failures),
    inFlight: alignToWindow(series.pending),
    succeeded: alignToWindow(series.succeeded),
    other: Array.from({ length: HOUR_COUNT }, () => 0),
  };
  const priority = ["failed", "inFlight", "succeeded"] as const;

  for (let index = 0; index < HOUR_COUNT; index += 1) {
    const total = Math.max(tasks[index], 0);
    // Clamped against the hour's own total so a series that disagrees with it
    // cannot draw a bar taller than the work it describes.
    let claimed = 0;
    for (const band of priority) {
      const amount = Math.min(Math.max(bands[band][index], 0), Math.max(total - claimed, 0));
      bands[band][index] = amount;
      claimed += amount;
    }
    bands.other[index] = Math.max(total - claimed, 0);
    tasks[index] = total;
  }

  return summarizeActivity(tasks, bands);
}

function summarizeActivity(tasks: number[], bands: AppRunActivity["bands"]): AppRunActivity {
  return {
    tasks,
    bands,
    totals: {
      failed: sum(bands.failed),
      inFlight: sum(bands.inFlight),
      other: sum(bands.other),
      succeeded: sum(bands.succeeded),
    },
    total: sum(tasks),
  };
}

/** Right-aligns a server series on the window: the last value is the current hour. */
function alignToWindow(values: number[]): number[] {
  if (values.length === HOUR_COUNT) return [...values];
  const padding = Array.from({ length: Math.max(HOUR_COUNT - values.length, 0) }, () => 0);
  return [...padding, ...values].slice(-HOUR_COUNT);
}

function emptyBands(): Record<TaskActivityBand, number[]> {
  const hours = () => Array.from({ length: HOUR_COUNT }, () => 0);
  return { failed: hours(), inFlight: hours(), other: hours(), succeeded: hours() };
}

function sum(values: number[]): number {
  return values.reduce((total, value) => total + value, 0);
}
