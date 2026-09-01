import { z } from "zod";

// Synced to packages/shared/src/shared/http/observability.py (LogRecord, LogQueryResponse)
// and packages/observability/src/observability/event_summary.py (ContainerEventSummary).
// Keep field names and defaults aligned with the Pydantic contracts.

export const logRecordSchema = z.object({
  id: z.string().default(""),
  cursor: z.string().default(""),
  seq_num: z.number().default(0),
  stored_at_ns: z.number().default(0),
  timestamp: z.string(),
  message: z.string(),
  stream: z.string().default(""),
  container_id: z.string().default(""),
  stub_id: z.string().default(""),
  stub_type: z.string().default(""),
  task_id: z.string().default(""),
  workspace_id: z.string().default(""),
  app_id: z.string().default(""),
  deployment_id: z.string().default(""),
  machine_id: z.string().default(""),
  worker_id: z.string().default(""),
  pid: z.number().default(0),
  process_args: z.array(z.string()).default([]),
  process_cwd: z.string().default(""),
  process_seq: z.number().default(0),
});
export type LogRecord = z.infer<typeof logRecordSchema>;

export const logQuerySchema = z.object({
  data: z.array(logRecordSchema).default([]),
  next: z.string().default(""),
});
export type LogQuery = z.infer<typeof logQuerySchema>;

const containerLifecycleMetricSchema = z.object({
  event_id: z.string(),
  duration_ms: z.number(),
  start_time: z.string().nullish(),
  end_time: z.string().nullish(),
});
export type ContainerLifecycleMetric = z.infer<typeof containerLifecycleMetricSchema>;

export const containerEventSummarySchema = z.object({
  container_id: z.string(),
  task_id: z.string().nullish(),
  stub_id: z.string().nullish(),
  event_count: z.number(),
  summary: z.record(z.number()).default({}),
  lifecycle: z.array(containerLifecycleMetricSchema).default([]),
  missing: z.array(z.string()).default([]),
});
export type ContainerEventSummary = z.infer<typeof containerEventSummarySchema>;

export const containerEventSummaryOrNullSchema = containerEventSummarySchema.nullable();

export const containerMetricsPointSchema = z.object({
  timestamp: z.string(),
  sample_interval_ms: z.number().default(0),
  cpu_millicores: z.number().default(0),
  cpu_total_millicores: z.number().default(0),
  cpu_pct: z.number().default(0),
  memory_rss_bytes: z.number().default(0),
  memory_total_bytes: z.number().default(0),
  network_recv_bytes: z.number().default(0),
  network_sent_bytes: z.number().default(0),
  disk_read_bytes: z.number().default(0),
  disk_write_bytes: z.number().default(0),
  disk_used_bytes: z.number().default(0),
  disk_total_bytes: z.number().default(0),
  gpu_memory_used_bytes: z.number().default(0),
  gpu_memory_total_bytes: z.number().default(0),
  gpu_type: z.string().default(""),
});
export type ContainerMetricsPoint = z.infer<typeof containerMetricsPointSchema>;

export const containerMetricsTimeseriesSchema = z.object({
  container_id: z.string(),
  points: z.array(containerMetricsPointSchema).default([]),
});
export type ContainerMetricsTimeseries = z.infer<typeof containerMetricsTimeseriesSchema>;

// Synced to packages/shared/src/shared/http/observability.py
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

// Synced to packages/shared/src/shared/http/observability.py
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
