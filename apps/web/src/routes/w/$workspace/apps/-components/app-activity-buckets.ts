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

function emptyBands(): Record<TaskActivityBand, number[]> {
  const hours = () => Array.from({ length: HOUR_COUNT }, () => 0);
  return { failed: hours(), inFlight: hours(), other: hours(), succeeded: hours() };
}

function sum(values: number[]): number {
  return values.reduce((total, value) => total + value, 0);
}
