import { describe, expect, it } from "vitest";

import type { Schemas } from "@/lib/api/client";

import { appRunActivity } from "./app-activity-buckets";

function counts(partial: Partial<Schemas["TaskStatusCounts"]>): Schemas["TaskStatusCounts"] {
  return { queued: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0, ...partial };
}

function series(
  fn: string,
  buckets: { timestamp: string; counts: Partial<Schemas["TaskStatusCounts"]> }[],
): Schemas["ActivitySeries"] {
  return {
    app: "shop",
    function: fn,
    total: 0,
    buckets: buckets.map((bucket) => ({
      timestamp: bucket.timestamp,
      status_counts: counts(bucket.counts),
    })),
  };
}

describe("appRunActivity", () => {
  it("sums every function's series into the current 24 hourly slots", () => {
    const activity = appRunActivity(
      [
        series("greet", [
          { timestamp: "2026-07-10T11:00:00Z", counts: { succeeded: 3, failed: 1 } },
          { timestamp: "2026-07-10T12:00:00Z", counts: { failed: 1 } },
        ]),
        series("report", [{ timestamp: "2026-07-10T12:00:00Z", counts: { failed: 1 } }]),
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

  it("ignores invalid, future and expired buckets", () => {
    const activity = appRunActivity(
      [
        series("greet", [
          { timestamp: "invalid", counts: { failed: 8 } },
          { timestamp: "2026-07-09T12:00:00Z", counts: { failed: 2 } },
          { timestamp: "2026-07-10T13:00:00Z", counts: { failed: 1 } },
        ]),
      ],
      new Date("2026-07-10T12:00:00Z"),
    );

    expect(activity.total).toBe(0);
    expect(activity.totals.failed).toBe(0);
  });

  it("keeps unfinished and cancelled work out of the successful band", () => {
    const activity = appRunActivity(
      [
        series("greet", [
          {
            timestamp: "2026-08-24T22:00:00.000Z",
            counts: { queued: 3, running: 1, succeeded: 1, failed: 2, cancelled: 1 },
          },
        ]),
      ],
      new Date("2026-08-24T22:00:00.000Z"),
    );

    expect(activity.total).toBe(8);
    expect(activity.totals).toEqual({ failed: 2, inFlight: 4, other: 1, succeeded: 1 });
  });
});
