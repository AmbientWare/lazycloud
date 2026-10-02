import type { Schemas } from "@/lib/api/client";
import type { TaskActivityBand } from "@/lib/format";

const HOUR_MS = 60 * 60 * 1_000;
const HOUR_COUNT = 24;

export type AppRunActivity = {
  tasks: number[];
  /** Per-hour counts per band, every status the server reported placed in one. */
  bands: Record<TaskActivityBand, number[]>;
  totals: Record<TaskActivityBand, number>;
  total: number;
};

/** Unfinished work is in flight, and a cancelled task neither failed nor succeeded. */
const STATUS_BANDS: Record<keyof Schemas["TaskStatusCounts"], TaskActivityBand> = {
  queued: "inFlight",
  running: "inFlight",
  succeeded: "succeeded",
  failed: "failed",
  cancelled: "other",
};

/**
 * Maps hourly activity buckets onto the current 24 UTC hours, summed over
 * the series: one per function for an app, or the app's own series.
 */
export function appRunActivity(
  series: readonly Schemas["ActivitySeries"][] | undefined,
  now = new Date(),
): AppRunActivity {
  const tasks = hours();
  const bands: Record<TaskActivityBand, number[]> = {
    failed: hours(),
    inFlight: hours(),
    other: hours(),
    succeeded: hours(),
  };
  const currentHour = Math.floor(now.getTime() / HOUR_MS) * HOUR_MS;
  const firstHour = currentHour - (HOUR_COUNT - 1) * HOUR_MS;

  for (const bucket of (series ?? []).flatMap((item) => item.buckets)) {
    const timestamp = new Date(bucket.timestamp).getTime();
    if (!Number.isFinite(timestamp)) continue;
    const index = Math.floor((Math.floor(timestamp / HOUR_MS) * HOUR_MS - firstHour) / HOUR_MS);
    if (index < 0 || index >= HOUR_COUNT) continue;
    for (const status of Object.keys(STATUS_BANDS) as (keyof typeof STATUS_BANDS)[]) {
      const amount = Math.max(Math.trunc(bucket.status_counts[status]), 0);
      tasks[index] += amount;
      bands[STATUS_BANDS[status]][index] += amount;
    }
  }

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

function hours(): number[] {
  return Array.from({ length: HOUR_COUNT }, () => 0);
}

function sum(values: number[]): number {
  return values.reduce((total, value) => total + value, 0);
}
