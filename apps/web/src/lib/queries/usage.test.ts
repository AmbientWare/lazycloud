import { describe, expect, it } from "vitest";

import { usageBillingOverviewQueryOptions, usageBillingWorkloadsQueryOptions } from "./usage";

const window = {
  start: "2026-07-01T00:00:00Z",
  end: "2026-07-02T00:00:00Z",
};

describe("usage query recovery", () => {
  it("keeps active overview and workload detail in the bounded recovery pass", () => {
    expect(usageBillingOverviewQueryOptions("workspace-1", window, 3_600).meta).toMatchObject({
      workspaceLiveEnabled: true,
      workspaceLiveCritical: true,
    });
    expect(
      usageBillingWorkloadsQueryOptions("workspace-1", "app-1", window, 3_600).meta,
    ).toMatchObject({
      workspaceLiveEnabled: true,
      workspaceLiveCritical: true,
    });
  });

  it("keys and refreshes the backend-owned current period without browser dates", () => {
    const options = usageBillingOverviewQueryOptions("workspace-1", { period: "current" }, 86_400);

    expect(options.queryKey).toContainEqual({
      period: "current",
      start: null,
      end: null,
    });
    expect(options.refetchInterval).toBe(60_000);
  });
});
