import type { Schemas } from "@/lib/api/client";

type BilledDimension = Schemas["BilledDimension"];

/** Every invoice line, in the order the totals and the chart's stack read them. */
export const BILLED_DIMENSIONS = [
  "compute_runtime",
  "network_egress",
  "volume_storage",
  "disk",
] as const satisfies readonly BilledDimension[];

export const COST_DIMENSIONS = {
  compute_runtime: { label: "Compute", color: "var(--chart-2)" },
  volume_storage: { label: "Volumes & artifacts", color: "var(--chart-4)" },
  disk: { label: "Disks", color: "var(--chart-1)" },
  network_egress: { label: "Egress", color: "var(--chart-3)" },
} satisfies Record<BilledDimension, { label: string; color: string }>;

export const COST_COMPONENT_LABELS: Record<Schemas["UsageCostComponentKind"], string> = {
  container_time: "Container",
  cpu: "CPU",
  memory: "Memory",
  gpu: "GPU",
  egress: "Egress",
  volume_storage: "Volumes & artifacts",
  disk: "Disk",
};
