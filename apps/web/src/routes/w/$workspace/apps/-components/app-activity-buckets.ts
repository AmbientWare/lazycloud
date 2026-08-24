import { isTerminalTaskStatus, type TaskTimeWindowBucket } from "@/lib/api/schemas";

const HOUR_MS = 60 * 60 * 1_000;
const HOUR_COUNT = 24;

export type AppRunActivity = {
  tasks: number[];
  failures: number[];
  /** Tasks that have not reached a terminal status, counted per hour.

      Drawn as its own band rather than folded into the rest, because the rest
      is drawn as success: a task that has not run yet is not a task that
      succeeded, and an app whose every task is stuck reads as a healthy one. */
  pending: number[];
  total: number;
  failed: number;
  waiting: number;
};

/** Maps sparse server-owned hourly buckets onto the current 24 UTC hours. */
export function appRunActivity(
  buckets: TaskTimeWindowBucket[] | undefined,
  now = new Date(),
): AppRunActivity {
  const tasks = Array.from({ length: HOUR_COUNT }, () => 0);
  const failures = Array.from({ length: HOUR_COUNT }, () => 0);
  const pending = Array.from({ length: HOUR_COUNT }, () => 0);
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
    // Everything the server did not report as terminal. Summed from the status
    // map rather than derived as `count - terminal`, so a status this build has
    // never heard of lands in one of the two bands rather than in neither.
    let waitingHere = 0;
    for (const [status, value] of Object.entries(bucket.status_counts)) {
      if (isTerminalTaskStatus(status)) continue;
      waitingHere += Math.max(Math.trunc(value), 0);
    }
    tasks[index] += count;
    failures[index] += failed;
    pending[index] += Math.min(waitingHere, Math.max(count - failed, 0));
  }

  return {
    tasks,
    failures,
    pending,
    total: tasks.reduce((sum, value) => sum + value, 0),
    failed: failures.reduce((sum, value) => sum + value, 0),
    waiting: pending.reduce((sum, value) => sum + value, 0),
  };
}
