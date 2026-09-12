import { z } from "zod";

/** What a quote prices, at the granularity one invoice line covers. */
export const billedDimensions = ["compute_runtime", "network_egress", "volume_storage"] as const;
export type BilledDimension = (typeof billedDimensions)[number];

/** What a quote prices, at the granularity the rate card publishes. */
export const ledgerComponents = [
  "container_time",
  "cpu",
  "memory",
  "gpu",
  "egress",
  "volume_storage",
] as const;
export type LedgerComponent = (typeof ledgerComponents)[number];

export const usageCostGroupKeys = ["app", "workload", "task"] as const;
export type UsageCostGroupKey = (typeof usageCostGroupKeys)[number];

/**
 * One resource's share of a row, and the invoice line it rolls up into.
 *
 * Egress and volume storage arrive here priced at zero rather than absent, and
 * they are rendered that way: a $0.00 line says the traffic is measured and
 * free, where a missing line would say nobody was looking.
 */
export const usageCostComponentSchema = z.object({
  dimension: z.enum(billedDimensions),
  component: z.enum(ledgerComponents),
  quantity: z.number(),
  cost_nanos: z.number().int().nonnegative(),
});
export type UsageCostComponent = z.infer<typeof usageCostComponentSchema>;

/**
 * A name is empty where the app or workload has since been deleted. The cost was
 * still incurred, so the row keeps its id and says the name is gone rather than
 * carrying one invented to fill the column.
 *
 * What the row was charged for is in `components` and nowhere else, so a column
 * of the table and the breakdown beside it cannot state different figures.
 */
export const usageCostRowSchema = z.object({
  app_id: z.string().default(""),
  app_name: z.string().default(""),
  workspace_id: z.string().default(""),
  workspace_name: z.string().default(""),
  workload_id: z.string().default(""),
  workload_name: z.string().default(""),
  workload_kind: z.string().default(""),
  task_id: z.string().default(""),
  /** "image-build" for image builds, which reach no app; empty otherwise. */
  category: z.string().default(""),
  cost_nanos: z.number().int().nonnegative().default(0),
  components: z.array(usageCostComponentSchema).default([]),
});
export type UsageCostRow = z.infer<typeof usageCostRowSchema>;

/**
 * `cost_nanos` is the whole window's total, not this page's — a page that does
 * not sum to it has more behind it, and the figure shown as the bill never
 * depends on how far somebody scrolled.
 */
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

/**
 * One invoice line's share of an interval.
 *
 * A dimension nothing was metered in is absent rather than zero: over an
 * interval no ledger row was written at all, and a zero line would claim a
 * measurement nobody took.
 */
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

/**
 * An account's spend over a window, in the shape it took.
 *
 * Every interval the window covers is present, quiet ones included, so a chart
 * drawn straight from `data` cannot render a fortnight of nothing as a
 * fortnight of something. `cost_nanos` is those intervals summed.
 */
export const usageCostSeriesSchema = z.object({
  start: z.string(),
  end: z.string(),
  currency: z.string().regex(/^[A-Z]{3}$/),
  bucket: z.enum(usageCostBuckets),
  cost_nanos: z.number().int().nonnegative().default(0),
  data: z.array(usageCostBucketSchema).default([]),
});
export type UsageCostSeries = z.infer<typeof usageCostSeriesSchema>;
