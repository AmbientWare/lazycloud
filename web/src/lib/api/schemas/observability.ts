import { z } from "zod";

// Synced to python/shared/src/shared/http/observability.py
// (TaskLatencyBucketResponse, TaskLatencyTimeseriesResponse).
const taskLatencyBucketSchema = z.object({
  timestamp: z.string(),
  count: z.number().default(0),
  p50_ms: z.number().nullish(),
  p95_ms: z.number().nullish(),
  cold_starts: z.number().default(0),
  status_counts: z.record(z.number()).default({}),
});
export type TaskLatencyBucket = z.infer<typeof taskLatencyBucketSchema>;

export const taskLatencyTimeseriesSchema = z.object({
  workspace_id: z.string(),
  stub_ids: z.array(z.string()).default([]),
  deployment_id: z.string().default(""),
  window_seconds: z.number(),
  buckets: z.array(taskLatencyBucketSchema).default([]),
});
export type TaskLatencyTimeseries = z.infer<typeof taskLatencyTimeseriesSchema>;

// Synced to python/shared/src/shared/http/observability.py
// (AccountContainerCountsResponse, AccountActivityResponse).
export const accountActivityMeasures = ["containers", "tasks", "cpu", "memory", "gpu"] as const;
export type AccountActivityMeasure = (typeof accountActivityMeasures)[number];

/**
 * What a reading is denominated in. `starts` counts events inside an interval;
 * the rest are capacity held across it, averaged over the seconds that interval
 * actually covers.
 */
export const accountActivityUnits = ["starts", "cores", "gibibytes", "gpus"] as const;
export type AccountActivityUnit = (typeof accountActivityUnits)[number];

export const accountActivitySeriesKinds = ["app", "unassigned", "other"] as const;
export type AccountActivitySeriesKind = (typeof accountActivitySeriesKinds)[number];

/** Only the live statuses: what the account occupies now, not what it has run. */
export const accountContainerCountsSchema = z.object({
  pending: z.number().int().nonnegative().default(0),
  running: z.number().int().nonnegative().default(0),
});
export type AccountContainerCounts = z.infer<typeof accountContainerCountsSchema>;

const accountActivityBucketSchema = z.object({
  timestamp: z.string(),
  value: z.number().nonnegative().default(0),
});
export type AccountActivityBucket = z.infer<typeof accountActivityBucketSchema>;

/**
 * A series is one app in one workspace. `workspace_id` leads the identity
 * because an account reads several workspaces at once and two of them may hold
 * apps of the same name.
 *
 * `app_name` is empty where the app has since been deleted, and for the
 * `unassigned` and `other` rows, which stand for no single app.
 */
const accountActivitySeriesSchema = z.object({
  kind: z.enum(accountActivitySeriesKinds),
  workspace_id: z.string().default(""),
  workspace_name: z.string().default(""),
  app_id: z.string().default(""),
  app_name: z.string().default(""),
  total: z.number().nonnegative().default(0),
  buckets: z.array(accountActivityBucketSchema).default([]),
});
export type AccountActivitySeries = z.infer<typeof accountActivitySeriesSchema>;

/**
 * `total` reads the whole window in the same unit every bucket is in, so a
 * series total is comparable against it and the shares add up.
 */
export const accountActivitySchema = z.object({
  measure: z.enum(accountActivityMeasures),
  unit: z.enum(accountActivityUnits),
  window_seconds: z.number().int().positive(),
  start: z.string(),
  end: z.string(),
  total: z.number().nonnegative().default(0),
  series: z.array(accountActivitySeriesSchema).default([]),
});
export type AccountActivity = z.infer<typeof accountActivitySchema>;
