import { formatDuration } from "@/lib/format";

/**
 * Display metadata for usage/billing metrics: a human label plus a quantity
 * formatter, so pages never render raw backend metric names such as
 * `container_duration_milliseconds`.
 */
export type MetricDisplay = {
  label: string;
  format: (quantity: number) => string;
  /** Backend/operator series that never surface on customer-facing pages. */
  internal: boolean;
  /** Stable ordering for summary readouts; lower renders first. */
  order: number;
};

function formatCount(quantity: number): string {
  return Intl.NumberFormat().format(Math.round(quantity));
}

function formatCents(quantity: number): string {
  return `$${(quantity / 100).toFixed(2)}`;
}

function formatSeconds(quantity: number): string {
  return formatDuration(quantity * 1_000);
}

function formatBytes(quantity: number): string {
  if (quantity < 1024) return `${formatCount(quantity)} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"] as const;
  let value = quantity / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

function formatGibSeconds(quantity: number): string {
  return `${Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(quantity)} GiB·s`;
}

const KNOWN_METRICS: Record<string, Omit<MetricDisplay, "internal"> & { internal?: boolean }> = {
  container_duration_milliseconds: { label: "Compute time", format: formatDuration, order: 0 },
  gpu_seconds: { label: "GPU time", format: formatSeconds, order: 1 },
  cpu_seconds: { label: "CPU time", format: formatSeconds, order: 2 },
  memory_gib_seconds: { label: "Memory", format: formatGibSeconds, order: 3 },
  task_count: { label: "Tasks", format: formatCount, order: 4 },
  container_cost_cents: { label: "Compute cost", format: formatCents, order: 5 },
  managed_compute_reservation_seconds: {
    label: "Reserved compute",
    format: formatSeconds,
    order: 6,
  },
  managed_compute_reservation_cost_cents: {
    label: "Reserved compute cost",
    format: formatCents,
    order: 7,
  },
  customer_cloud_management_seconds: {
    label: "Customer cloud management",
    format: formatSeconds,
    order: 8,
  },
  storage_bytes: { label: "Storage", format: formatBytes, order: 9 },
  // Scheduler/operator counters: never rendered on customer pages.
  container_requested_count: { label: "Containers requested", format: formatCount, order: 100, internal: true },
  container_scheduled_count: { label: "Containers scheduled", format: formatCount, order: 101, internal: true },
  node_usage: { label: "Node usage", format: formatCount, order: 102, internal: true },
  customer_cloud_management_cost_cents: { label: "Customer cloud management cost", format: formatCents, order: 103, internal: true },
  customer_cloud_allocated_cpu_seconds: { label: "Customer cloud CPU allocation", format: formatSeconds, order: 104, internal: true },
  customer_cloud_allocated_memory_gib_seconds: { label: "Customer cloud memory allocation", format: formatGibSeconds, order: 105, internal: true },
  customer_cloud_allocated_gpu_seconds: { label: "Customer cloud GPU allocation", format: formatSeconds, order: 106, internal: true },
  customer_cloud_allocated_disk_gib_seconds: { label: "Customer cloud disk allocation", format: formatGibSeconds, order: 107, internal: true },
  customer_cloud_network_ingress_bytes: { label: "Customer cloud network ingress", format: formatBytes, order: 108, internal: true },
  customer_cloud_network_egress_bytes: { label: "Customer cloud network egress", format: formatBytes, order: 109, internal: true },
};

const UNIT_FORMATTERS: Record<string, (quantity: number) => string> = {
  milliseconds: formatDuration,
  seconds: formatSeconds,
  count: formatCount,
  cents: formatCents,
  bytes: formatBytes,
  gib_seconds: formatGibSeconds,
};

/** `container_duration_milliseconds` -> `Container duration`. */
function humanizeMetricName(metric: string): string {
  const words = metric
    .split("_")
    .filter(
      (word, index, all) =>
        word.length > 0 && !(index === all.length - 1 && word in UNIT_SUFFIX_WORDS),
    );
  const sentence = words.join(" ") || metric;
  return sentence.charAt(0).toUpperCase() + sentence.slice(1);
}

const UNIT_SUFFIX_WORDS: Record<string, true> = {
  milliseconds: true,
  seconds: true,
  count: true,
  cents: true,
  bytes: true,
};

export function metricDisplay(metric: string, unit?: string): MetricDisplay {
  const known = KNOWN_METRICS[metric];
  if (known) return { internal: false, ...known };
  return {
    label: humanizeMetricName(metric),
    format: (unit && UNIT_FORMATTERS[unit]) || formatCount,
    internal: false,
    order: 50,
  };
}

/** Compact duration for chart axis ticks: "45s", "12m", "1.5h". */
export function formatDurationCompact(milliseconds: number): string {
  if (milliseconds <= 0) return "0";
  const seconds = milliseconds / 1_000;
  if (seconds < 60) return `${seconds < 10 ? Number(seconds.toFixed(1)) : Math.round(seconds)}s`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${minutes < 10 ? Number(minutes.toFixed(1)) : Math.round(minutes)}m`;
  const hours = minutes / 60;
  return `${hours < 10 ? Number(hours.toFixed(1)) : Math.round(hours)}h`;
}
