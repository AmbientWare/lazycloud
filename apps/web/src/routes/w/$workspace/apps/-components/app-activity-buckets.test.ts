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
    expect(activity.failures.slice(-2)).toEqual([1, 2]);
    expect(activity.total).toBe(6);
    expect(activity.failed).toBe(3);
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
    expect(activity.failed).toBe(0);
  });
});

describe("appRunActivity pending band", () => {
  it("does not count a pending task as a successful one", () => {

    const now = new Date("2026-08-24T22:00:00.000Z");
    const activity = appRunActivity(
      [
        {
          timestamp: "2026-08-24T22:00:00.000Z",
          count: 4,
          status_counts: { pending: 3, complete: 1 },
        },
      ],
      now,
    );

    const hour = activity.tasks.length - 1;
    expect(activity.tasks[hour]).toBe(4);
    expect(activity.pending[hour]).toBe(3);
    expect(activity.failures[hour]).toBe(0);
    // What the bar paints green: total less failed less pending.
    expect(activity.tasks[hour] - activity.failures[hour] - activity.pending[hour]).toBe(1);
    expect(activity.waiting).toBe(3);
  });
});
