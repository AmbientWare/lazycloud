import type { TaskTimeWindowBucket } from "@/lib/api/schemas";

const HOUR_MS = 60 * 60 * 1_000;
const HOUR_COUNT = 24;

export type AppRunActivity = {
  tasks: number[];
  failures: number[];
  total: number;
  failed: number;
};

/** Maps sparse server-owned hourly buckets onto the current 24 UTC hours. */
export function appRunActivity(
  buckets: TaskTimeWindowBucket[] | undefined,
  now = new Date(),
): AppRunActivity {
  const tasks = Array.from({ length: HOUR_COUNT }, () => 0);
  const failures = Array.from({ length: HOUR_COUNT }, () => 0);
  const currentHour = Math.floor(now.getTime() / HOUR_MS) * HOUR_MS;
  const firstHour = currentHour - (HOUR_COUNT - 1) * HOUR_MS;

  for (const bucket of buckets ?? []) {
    const timestamp = new Date(bucket.timestamp).getTime();
    if (!Number.isFinite(timestamp)) continue;
    const hour = Math.floor(timestamp / HOUR_MS) * HOUR_MS;
    const index = Math.floor((hour - firstHour) / HOUR_MS);
    if (index < 0 || index >= HOUR_COUNT) continue;

    const count = Math.max(Math.trunc(bucket.count), 0);
    const failed = Math.min(Math.max(Math.trunc(bucket.status_counts.failed ?? 0), 0), count);
    tasks[index] += count;
    failures[index] += failed;
  }

  return {
    tasks,
    failures,
    total: tasks.reduce((sum, value) => sum + value, 0),
    failed: failures.reduce((sum, value) => sum + value, 0),
  };
}
