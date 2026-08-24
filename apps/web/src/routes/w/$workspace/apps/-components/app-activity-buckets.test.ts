import { describe, expect, it } from "vitest";

import { appRunActivity } from "./app-activity-buckets";

describe("appRunActivity", () => {
  it("fills the current 24 hourly slots from sparse app-scoped buckets", () => {
    const activity = appRunActivity(
      [
        {
          timestamp: "2026-07-10T11:15:00Z",
          count: 4,
          status_counts: { complete: 3, failed: 1 },
        },
        {
          timestamp: "2026-07-10T12:00:00Z",
          count: 2,
          status_counts: { failed: 2 },
        },
      ],
      new Date("2026-07-10T12:59:00Z"),
    );

    expect(activity.tasks).toHaveLength(24);
    expect(activity.tasks.slice(-2)).toEqual([4, 2]);
    expect(activity.bands.failed.slice(-2)).toEqual([1, 2]);
    expect(activity.bands.succeeded.slice(-2)).toEqual([3, 0]);
    expect(activity.total).toBe(6);
    expect(activity.totals.failed).toBe(3);
  });

  it("ignores invalid, future, and expired buckets", () => {
    const activity = appRunActivity(
      [
        { timestamp: "invalid", count: 8, status_counts: { failed: 8 } },
        { timestamp: "2026-07-09T12:00:00Z", count: 5, status_counts: { failed: 2 } },
        { timestamp: "2026-07-10T13:00:00Z", count: 7, status_counts: { failed: 1 } },
      ],
      new Date("2026-07-10T12:00:00Z"),
    );

    expect(activity.total).toBe(0);
    expect(activity.totals.failed).toBe(0);
  });

  it("keeps unfinished, cancelled, and unknown work out of the successful band", () => {
    const activity = appRunActivity(
      [
        {
          timestamp: "2026-08-24T22:00:00.000Z",
          count: 9,
          status_counts: {
            pending: 3,
            running: 1,
            retry: 1,
            complete: 1,
            timeout: 1,
            cancelled: 1,
            reticulating: 1,
          },
        },
      ],
      new Date("2026-08-24T22:00:00.000Z"),
    );

    expect(activity.total).toBe(9);
    expect(activity.totals).toEqual({ failed: 1, inFlight: 5, other: 2, succeeded: 1 });
  });
});
