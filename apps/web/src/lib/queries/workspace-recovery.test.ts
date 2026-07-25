import { describe, expect, it } from "vitest";

import { concurrencyLimitsQueryOptions } from "./concurrency";
import { cronJobsQueryOptions } from "./cron";
import { secretsQueryOptions, volumesQueryOptions } from "./storage";
import { taskLatencyQueryOptions } from "./stubs";
import { taskBucketsQueryOptions, taskMetricsQueryOptions } from "./tasks";

describe("event-only workspace queries", () => {
  it.each([
    concurrencyLimitsQueryOptions("workspace-1"),
    cronJobsQueryOptions("workspace-1"),
    secretsQueryOptions("workspace-1"),
    volumesQueryOptions("workspace-1"),
    taskMetricsQueryOptions("workspace-1"),
    taskBucketsQueryOptions("workspace-1"),
    taskLatencyQueryOptions("workspace-1", ["stub-1"]),
  ])("closes the initial stream-tail race through critical recovery", (options) => {
    expect(options.meta).toMatchObject({
      workspaceLiveEnabled: true,
      workspaceLiveCritical: true,
    });
    expect(options).not.toHaveProperty("refetchInterval");
  });
});
