import { z } from "zod";

import { jsonValueSchema } from "./json";

export const usageBillingPeriods = ["current"] as const;
export type UsageBillingPeriod = (typeof usageBillingPeriods)[number];

export const usageMetrics = [
  "container_requested_count",
  "container_scheduled_count",
  "cpu_seconds",
  "memory_gib_seconds",
  "gpu_seconds",
  "task_count",
  "persistent_volume_byte_seconds",
  "container_duration_milliseconds",
  "container_disk_byte_seconds",
  "cpu_used_core_seconds",
  "memory_rss_byte_seconds",
  "memory_swap_byte_seconds",
  "gpu_memory_byte_seconds",
  "network_ingress_bytes",
  "network_egress_bytes",
  "network_ingress_packets",
  "network_egress_packets",
  "disk_read_bytes",
  "disk_write_bytes",
  "node_usage",
] as const;

export const usageUnits = [
  "seconds",
  "count",
  "bytes",
  "gib_seconds",
  "milliseconds",
  "byte_seconds",
] as const;

export const billableMetrics = [
  "cpu_seconds",
  "memory_gib_seconds",
  "gpu_seconds",
  "managed_cpu_seconds",
  "managed_memory_gib_seconds",
  "managed_gpu_seconds",
] as const;

const currencySchema = z.string().regex(/^[A-Z]{3}$/);

export const usageRecordSchema = z.object({
  id: z.string(),
  workspace_id: z.string(),
  resource_type: z.string(),
  resource_id: z.string(),
  metric: z.enum(usageMetrics),
  quantity: z.number().nonnegative(),
  unit: z.enum(usageUnits),
  labels: z.record(z.string(), z.string()).default({}),
  metadata: z.record(jsonValueSchema).default({}),
  created_at: z.string(),
});
export type UsageRecord = z.infer<typeof usageRecordSchema>;

export const usageRecordListSchema = z.object({
  data: z.array(usageRecordSchema).default([]),
  next: z.string().default(""),
});
export type UsageRecordList = z.infer<typeof usageRecordListSchema>;

const usageBillingLineSchema = z.object({
  metric: z.enum(billableMetrics),
  label: z.string(),
  quantity: z.number().nonnegative(),
  unit: z.enum(usageUnits),
  price_per_unit_nanos: z.number().int().nonnegative().nullish(),
  cost_nanos: z.number().int().nonnegative(),
  // A summary can carry several lines sharing a metric — one per GPU model — so
  // metric alone does not identify a line, and neither does metric and variant:
  // a rate change splits one metric into two lines. Readers key on all three.
  variant: z.string().default(""),
  // A rate change splits a metric into two lines rather than blending them; this
  // is what distinguishes them. Null where nothing priced the line.
  effective_date: z.string().nullable().default(null),
});
export type UsageBillingLine = z.infer<typeof usageBillingLineSchema>;

export const usageBillingAttributionSchema = z.object({
  app_id: z.string().default(""),
  app_name: z.string().default("Unlinked"),
  workload_id: z.string().default(""),
  workload_name: z.string().default(""),
  workload_kind: z.string().default(""),
  tasks: z.number().int().nonnegative().default(0),
  total_cost_nanos: z.number().int().nonnegative().default(0),
  lines: z.array(usageBillingLineSchema).default([]),
});
export type UsageBillingAttribution = z.infer<typeof usageBillingAttributionSchema>;

export const usageBillingAppSummarySchema = z.object({
  app_id: z.string().default(""),
  app_name: z.string().default("Unlinked"),
  tasks: z.number().int().nonnegative().default(0),
  total_cost_nanos: z.number().int().nonnegative().default(0),
  lines: z.array(usageBillingLineSchema).default([]),
});
export type UsageBillingAppSummary = z.infer<typeof usageBillingAppSummarySchema>;

const usageBillingBucketSchema = z.object({
  start: z.string(),
  end: z.string(),
  total_cost_nanos: z.number().int().nonnegative().default(0),
  lines: z.array(usageBillingLineSchema).default([]),
});

export const usageBillingOverviewSchema = z.object({
  workspace_id: z.string(),
  start: z.string(),
  end: z.string(),
  currency: currencySchema,
  total_cost_nanos: z.number().int().nonnegative().default(0),
  summary: z.array(usageBillingLineSchema).default([]),
  apps: z.array(usageBillingAppSummarySchema).default([]),
  activity: z.array(usageBillingBucketSchema).default([]),
});
export type UsageBillingOverview = z.infer<typeof usageBillingOverviewSchema>;

export const usageBillingWorkloadListSchema = z.object({
  workspace_id: z.string(),
  app_id: z.string(),
  start: z.string(),
  end: z.string(),
  currency: currencySchema,
  data: z.array(usageBillingAttributionSchema).default([]),
});
export type UsageBillingWorkloadList = z.infer<typeof usageBillingWorkloadListSchema>;
