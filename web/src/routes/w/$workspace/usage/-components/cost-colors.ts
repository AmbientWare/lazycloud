import type { BilledDimension, UsageCostComponentKind } from "@/lib/api/schemas";

export const COST_DIMENSIONS = {
  compute_runtime: { label: "Compute", color: "var(--chart-2)" },
  volume_storage: { label: "Volumes & artifacts", color: "var(--chart-4)" },
  disk: { label: "Disks", color: "var(--chart-1)" },
  network_egress: { label: "Egress", color: "var(--chart-3)" },
} satisfies Record<BilledDimension, { label: string; color: string }>;

export const COST_COMPONENT_LABELS: Record<UsageCostComponentKind, string> = {
  container_time: "Container",
  cpu: "CPU",
  memory: "Memory",
  gpu: "GPU",
  egress: "Egress",
  volume_storage: "Volumes & artifacts",
  disk: "Disk",
};
