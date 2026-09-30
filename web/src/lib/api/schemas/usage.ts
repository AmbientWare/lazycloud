import { z } from "zod";

/** What a quote prices, at the granularity one invoice line covers. */
export const billedDimensions = [
  "compute_runtime",
  "network_egress",
  "volume_storage",
  "disk",
] as const;
export type BilledDimension = (typeof billedDimensions)[number];

/** A resource as the customer is charged for it; a disk is one charge. */
export const usageCostComponents = [
  "container_time",
  "cpu",
  "memory",
  "gpu",
  "egress",
  "volume_storage",
  "disk",
] as const;
export type UsageCostComponentKind = (typeof usageCostComponents)[number];

export const usageCostGroupKeys = ["app", "workload", "task"] as const;
export type UsageCostGroupKey = (typeof usageCostGroupKeys)[number];

export type UsageCostCategory = "image-build" | "disk" | "unattributed";

/** A zero cost can represent measured usage with no charge. */
export const usageCostComponentSchema = z.object({
  dimension: z.enum(billedDimensions),
  component: z.enum(usageCostComponents),
  quantity: z.number(),
  cost_nanos: z.number().int().nonnegative(),
});
export type UsageCostComponent = z.infer<typeof usageCostComponentSchema>;

/** Deleted resources retain their IDs and costs, with empty names. */
export const usageCostRowSchema = z.object({
  app_id: z.string().default(""),
  app_name: z.string().default(""),
  workspace_id: z.string().default(""),
  workspace_name: z.string().default(""),
  workload_id: z.string().default(""),
  workload_name: z.string().default(""),
  workload_kind: z.string().default(""),
  task_id: z.string().default(""),
  disk_id: z.string().default(""),
  /** Empty where the disk has since been deleted. */
  disk_name: z.string().default(""),
  /** "image-build" for image builds and "disk" for one disk, neither of which reaches an app. */
  category: z.string().default(""),
  cost_nanos: z.number().int().nonnegative().default(0),
  components: z.array(usageCostComponentSchema).default([]),
});
export type UsageCostRow = z.infer<typeof usageCostRowSchema>;

/** `cost_nanos` covers the full filtered window across all pages. */
export const usageCostListSchema = z.object({
  /** Empty where the page covers an account rather than one workspace. */
  workspace_id: z.string().default(""),
  start: z.string(),
  end: z.string(),
  currency: z.string().regex(/^[A-Z]{3}$/),
  group_by: z.enum(usageCostGroupKeys),
  cost_nanos: z.number().int().nonnegative().default(0),
  data: z.array(usageCostRowSchema).default([]),
  next: z.string().default(""),
});
export type UsageCostList = z.infer<typeof usageCostListSchema>;

/** How wide one interval of a cost series is. */
export const usageCostBuckets = ["hour", "day"] as const;
export type UsageCostBucket = (typeof usageCostBuckets)[number];

/** A dimension is absent when it has no metered usage in the interval. */
export const usageCostDimensionTotalSchema = z.object({
  dimension: z.enum(billedDimensions),
  cost_nanos: z.number().int().nonnegative().default(0),
});
export type UsageCostDimensionTotal = z.infer<typeof usageCostDimensionTotalSchema>;

/** What one interval of the window cost, with its end clipped to the window. */
export const usageCostBucketSchema = z.object({
  started_at: z.string(),
  ended_at: z.string(),
  cost_nanos: z.number().int().nonnegative().default(0),
  dimensions: z.array(usageCostDimensionTotalSchema).default([]),
});
export type UsageCostBucketRow = z.infer<typeof usageCostBucketSchema>;

/** Includes empty intervals; `cost_nanos` is the sum of all interval costs. */
export const usageCostSeriesSchema = z.object({
  subscription_credit_nanos: z.number().int().nonnegative(),
  start: z.string(),
  end: z.string(),
  currency: z.string().regex(/^[A-Z]{3}$/),
  bucket: z.enum(usageCostBuckets),
  cost_nanos: z.number().int().nonnegative().default(0),
  data: z.array(usageCostBucketSchema).default([]),
});
export type UsageCostSeries = z.infer<typeof usageCostSeriesSchema>;
