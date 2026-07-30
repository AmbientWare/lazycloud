import { z } from "zod";

// Synced to packages/shared/src/shared/http/observability.py (LogRecord, LogQueryResponse)
// and packages/observability/src/observability/event_summary.py (ContainerEventSummary).
// Keep field names and defaults aligned with the Pydantic contracts.

export const logRecordSchema = z.object({
  id: z.string().default(""),
  cursor: z.string().default(""),
  seq_num: z.number().default(0),
  timestamp: z.string(),
  message: z.string(),
  stream: z.string().default(""),
  container_id: z.string().default(""),
  stub_id: z.string().default(""),
  task_id: z.string().default(""),
  app_id: z.string().default(""),
});
export type LogRecord = z.infer<typeof logRecordSchema>;

export const logQuerySchema = z.object({
  data: z.array(logRecordSchema).default([]),
  next: z.string().default(""),
  count: z.number().default(0),
  total_expected: z.number().default(0),
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
