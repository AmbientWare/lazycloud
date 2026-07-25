import { format, parseISO } from "date-fns";

import type { ContainerMetricsPoint } from "@/lib/api/schemas";

export type MetricDatum = {
  timestamp: string;
  label: string;
  cpuPercent: number;
  cpuUsed: number;
  cpuTotal: number;
  memoryUsed: number;
  memoryTotal: number;
  gpuMemoryUsed: number;
  gpuMemoryTotal: number;
  /** Bytes per second over the sample interval; null for samples without one. */
  networkRecvRate: number | null;
  networkSentRate: number | null;
  diskReadRate: number | null;
  diskWriteRate: number | null;
};

/**
 * Chronological chart data from raw worker samples. Network/disk counters are
 * per-interval deltas, so rates divide by the sample interval; samples recorded
 * before the interval existed get null rates (a gap, not a fake zero).
 */
export function buildMetricData(points: ContainerMetricsPoint[]): MetricDatum[] {
  return [...points]
    .sort((a, b) => a.timestamp.localeCompare(b.timestamp))
    .map((point) => {
      const intervalSeconds = point.sample_interval_ms > 0 ? point.sample_interval_ms / 1000 : null;
      return {
        timestamp: point.timestamp,
        label: formatSampleTime(point.timestamp),
        cpuPercent: cpuPercent(point.cpu_millicores, point.cpu_total_millicores),
        cpuUsed: point.cpu_millicores,
        cpuTotal: point.cpu_total_millicores,
        memoryUsed: point.memory_rss_bytes,
        memoryTotal: point.memory_total_bytes,
        gpuMemoryUsed: point.gpu_memory_used_bytes,
        gpuMemoryTotal: point.gpu_memory_total_bytes,
        networkRecvRate: perSecond(point.network_recv_bytes, intervalSeconds),
        networkSentRate: perSecond(point.network_sent_bytes, intervalSeconds),
        diskReadRate: perSecond(point.disk_read_bytes, intervalSeconds),
        diskWriteRate: perSecond(point.disk_write_bytes, intervalSeconds),
      };
    });
}

/** Whether any sample carries an interval, i.e. was recorded with I/O counters. */
export function hasIoSamples(points: ContainerMetricsPoint[]): boolean {
  return points.some((point) => point.sample_interval_ms > 0);
}

export type ComputeReadout = {
  cpu: string;
  memory: string;
  /** `null` until an interval-bearing sample provides real rates (no fake zeros). */
  network: string | null;
};

/**
 * Latest-sample readout strings for the compact compute strip headers. The
 * network readout stays null until the newest sample carries per-interval
 * rates, matching the gap semantics of the charts themselves.
 */
export function latestComputeReadout(data: MetricDatum[]): ComputeReadout | null {
  const latest = data[data.length - 1];
  if (!latest) return null;
  const network =
    latest.networkRecvRate !== null && latest.networkSentRate !== null
      ? `↓ ${formatBytesPerSecond(latest.networkRecvRate)} ↑ ${formatBytesPerSecond(latest.networkSentRate)}`
      : null;
  return {
    cpu: `${latest.cpuPercent.toFixed(1)}%`,
    memory: formatBytes(latest.memoryUsed),
    network,
  };
}

function cpuPercent(usedMillicores: number, totalMillicores: number): number {
  if (totalMillicores <= 0) return 0;
  return Math.min(100, (usedMillicores / totalMillicores) * 100);
}

export function formatBytes(value: number): string {
  if (value < 1024) return `${Math.round(value)} B`;
  const kib = value / 1024;
  if (kib < 1024) return `${kib.toFixed(1)} KiB`;
  const mib = kib / 1024;
  if (mib < 1024) return `${mib.toFixed(1)} MiB`;
  return `${(mib / 1024).toFixed(1)} GiB`;
}

export function formatBytesPerSecond(value: number): string {
  return `${formatBytes(value)}/s`;
}

function perSecond(bytes: number, intervalSeconds: number | null): number | null {
  if (intervalSeconds === null) return null;
  return bytes / intervalSeconds;
}

function formatSampleTime(timestamp: string): string {
  try {
    return format(parseISO(timestamp), "HH:mm:ss");
  } catch {
    return timestamp;
  }
}
