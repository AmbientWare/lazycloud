import { describe, expect, it } from "vitest";

import type { Schemas } from "@/lib/api/client";

import { buildMetricData, latestComputeReadout } from "./metrics";

function metrics(points: Partial<Schemas["ContainerMetricPoint"]>[]): Schemas["ContainerMetrics"] {
  return {
    container_id: "container-1",
    cpu_total_millicores: 1000,
    memory_total_bytes: 512 * 1024 * 1024,
    step_seconds: 5,
    points: points.map((point) => ({
      timestamp: "2026-07-09T12:00:00Z",
      interval_ms: 0,
      cpu_millicores: 250,
      memory_rss_bytes: 100 * 1024 * 1024,
      memory_swap_bytes: 0,
      network_recv_bytes: 0,
      network_sent_bytes: 0,
      disk_read_bytes: 0,
      disk_write_bytes: 0,
      ...point,
    })),
  };
}

describe("container metric chart data", () => {
  it("derives byte/s rates from per-interval counters", () => {
    const series = metrics([
      {
        timestamp: "2026-07-09T12:00:05Z",
        interval_ms: 5000,
        network_recv_bytes: 5 * 1024 * 1024,
        network_sent_bytes: 512 * 1024,
        disk_read_bytes: 10 * 1024,
      },
    ]);
    const [datum] = buildMetricData(series);
    expect(datum.networkRecvRate).toBe(1024 * 1024);
    expect(datum.networkSentRate).toBe((512 * 1024) / 5);
    expect(datum.diskReadRate).toBe(2 * 1024);
    expect(datum.diskWriteRate).toBe(0);
  });

  it("leaves gaps instead of fake zero rates for samples without an interval", () => {
    const series = metrics([{}]);
    const [datum] = buildMetricData(series);
    expect(datum.networkRecvRate).toBeNull();
    expect(datum.diskWriteRate).toBeNull();
    expect(datum.cpuPercent).toBe(25);
  });

  it("keeps the network readout null until interval-bearing samples arrive", () => {
    expect(latestComputeReadout(buildMetricData(metrics([{}])))).toEqual({
      cpu: "25.0%",
      memory: "100.0 MiB",
      network: null,
    });
    expect(latestComputeReadout([])).toBeNull();
  });
});
