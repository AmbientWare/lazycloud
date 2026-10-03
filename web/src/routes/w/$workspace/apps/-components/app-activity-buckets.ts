import { ACTIVITY_HOURS, activityHourIndex, activityHours } from "@/lib/activity-window";
import type { Schemas } from "@/lib/api/client";
import type { TaskActivityBand } from "@/lib/format";

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
  const drawn = activityHours(now.getTime());

  for (const bucket of (series ?? []).flatMap((item) => item.buckets)) {
    const index = activityHourIndex(bucket.timestamp, drawn);
    if (index < 0) continue;
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
  return Array.from({ length: ACTIVITY_HOURS }, () => 0);
}

function sum(values: number[]): number {
  return values.reduce((total, value) => total + value, 0);
}
