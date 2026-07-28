export function usageBillingOverviewFixture(workspaceId: string) {
  return {
    workspace_id: workspaceId,
    start: "2026-07-09T12:00:00Z",
    end: "2026-07-10T12:00:00Z",
    currency: "USD",
    total_cost_nanos: 526_000_000,
    contains_estimates: true,
    summary: billingLines(),
    apps: [
      {
        app_id: "app-1",
        app_name: "trainer",
        tasks: 2,
        total_cost_nanos: 526_000_000,
        lines: billingLines(),
      },
    ],
    activity: [
      {
        start: "2026-07-10T10:00:00Z",
        end: "2026-07-10T11:00:00Z",
        total_cost_nanos: 210_400_000,
        lines: billingLines(0.4),
      },
      {
        start: "2026-07-10T11:00:00Z",
        end: "2026-07-10T12:00:00Z",
        total_cost_nanos: 315_600_000,
        lines: billingLines(0.6),
      },
    ],
  };
}

export function usageBillingWorkloadsFixture(workspaceId: string) {
  return {
    workspace_id: workspaceId,
    app_id: "app-1",
    start: "2026-07-09T12:00:00Z",
    end: "2026-07-10T12:00:00Z",
    currency: "USD",
    data: [
      {
        app_id: "app-1",
        app_name: "trainer",
        workload_id: "stub-1",
        workload_name: "embed",
        workload_kind: "function",
        tasks: 2,
        total_cost_nanos: 526_000_000,
        lines: billingLines(),
      },
    ],
  };
}

export function emptyUsageBillingOverviewFixture(workspaceId: string) {
  return {
    workspace_id: workspaceId,
    start: "2026-07-09T00:00:00Z",
    end: "2026-07-10T00:00:00Z",
    currency: "USD",
    total_cost_nanos: 0,
    contains_estimates: false,
    summary: [],
    apps: [],
    activity: [],
  };
}

function billingLines(scale = 1) {
  return [
    line(
      "cpu_seconds",
      "CPU",
      3600 * scale,
      "seconds",
      11_244,
      36_000_000 * scale,
      "recorded_allocation",
    ),
    line(
      "memory_gib_seconds",
      "Memory",
      7200 * scale,
      "gib_seconds",
      1_235,
      4_000_000 * scale,
      "recorded_allocation",
    ),
    line(
      "gpu_seconds",
      "GPU",
      3600 * scale,
      "seconds",
      146_111,
      486_000_000 * scale,
      "recorded_allocation",
    ),
  ];
}

function line(
  metric: string,
  label: string,
  quantity: number,
  unit: string,
  pricePerUnitNanos: number,
  costNanos: number,
  costBasis: string,
) {
  return {
    metric,
    label,
    quantity,
    unit,
    price_per_unit_nanos: pricePerUnitNanos,
    cost_nanos: Math.round(costNanos),
    cost_basis: costBasis,
  };
}
