import { describe, expect, it } from "vitest";

import { containerMetricsPointSchema } from "@/lib/api/schemas";

import { buildMetricData, hasIoSamples, latestComputeReadout } from "./metrics";

const base = {
  cpu_millicores: 250,
  cpu_total_millicores: 1000,
  memory_rss_bytes: 100 * 1024 * 1024,
  memory_total_bytes: 512 * 1024 * 1024,
};

describe("container metric chart data", () => {
  it("derives byte/s rates from per-interval counters", () => {
    const points = [
      containerMetricsPointSchema.parse({
        timestamp: "2026-07-09T12:00:05Z",
        ...base,
        sample_interval_ms: 5000,
        network_recv_bytes: 5 * 1024 * 1024,
        network_sent_bytes: 512 * 1024,
        disk_read_bytes: 10 * 1024,
        disk_write_bytes: 0,
      }),
    ];
    const [datum] = buildMetricData(points);
    expect(datum.networkRecvRate).toBe(1024 * 1024);
    expect(datum.networkSentRate).toBe((512 * 1024) / 5);
    expect(datum.diskReadRate).toBe(2 * 1024);
    expect(datum.diskWriteRate).toBe(0);
    expect(hasIoSamples(points)).toBe(true);
  });

  it("leaves gaps instead of fake zero rates for pre-interval samples", () => {
    const points = [
      containerMetricsPointSchema.parse({ timestamp: "2026-07-09T12:00:00Z", ...base }),
    ];
    const [datum] = buildMetricData(points);
    expect(datum.networkRecvRate).toBeNull();
    expect(datum.diskWriteRate).toBeNull();
    expect(datum.cpuPercent).toBe(25);
    expect(hasIoSamples(points)).toBe(false);
  });




  it("keeps the network readout null until interval-bearing samples arrive", () => {
    const data = buildMetricData([
      containerMetricsPointSchema.parse({ timestamp: "2026-07-09T12:00:00Z", ...base }),
    ]);
    expect(latestComputeReadout(data)).toEqual({
      cpu: "25.0%",
      memory: "100.0 MiB",
      network: null,
    });
    expect(latestComputeReadout([])).toBeNull();
  });
});
