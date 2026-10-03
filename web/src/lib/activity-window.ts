const HOUR_MS = 3_600_000;

/** Activity charts draw the current UTC hour and the 23 before it. */
export const ACTIVITY_HOURS = 24;

/** The start of each drawn hour in epoch milliseconds, oldest first. */
export function activityHours(now = Date.now()): number[] {
  const current = Math.floor(now / HOUR_MS) * HOUR_MS;
  return Array.from(
    { length: ACTIVITY_HOURS },
    (_, index) => current - (ACTIVITY_HOURS - 1 - index) * HOUR_MS,
  );
}

/**
 * Where an activity read starts, computed per fetch and kept out of query
 * keys, which would otherwise change every hour.
 */
export function activityStart(now = Date.now()): string {
  return new Date(activityHours(now)[0]).toISOString();
}

/** The drawn hour a timestamp falls in, or -1 outside the window. */
export function activityHourIndex(timestamp: string, hours: readonly number[]): number {
  const time = Date.parse(timestamp);
  if (!Number.isFinite(time) || hours.length === 0) return -1;
  const index = Math.floor((time - hours[0]) / HOUR_MS);
  return index >= 0 && index < hours.length ? index : -1;
}
