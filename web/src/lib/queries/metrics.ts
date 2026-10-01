import { queryOptions } from "@tanstack/react-query";

import {
  taskLatencyTimeseriesSchema,
  taskMetricsSummarySchema,
  taskTimeWindowBucketListSchema,
} from "@/lib/api/schemas";
import { apiRequest, withWorkspace } from "@/lib/api/unserved";

import { workspaceQueryKeys } from "./workspace-keys";

/*
 * Task aggregates: activity over time, latency percentiles and the 24 hour
 * summary. The observability packet serves them.
 */

/** Buckets the activity chart renders; it slices to this and discards the rest. */
const BUCKETS_DRAWN = 24;

export function taskBucketsQueryOptions(
  workspace: string,
  windowSeconds = 3600,
  scope: { app?: string; function?: string } = {},
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.tasks.buckets(
      workspace,
      windowSeconds,
      scope.app ?? null,
      scope.function ?? null,
    ),
    queryFn: () => {
      const endedAt = Math.floor(Date.now() / 1000);
      const params = new URLSearchParams();
      params.set("window_seconds", String(windowSeconds));
      params.set("started_at", String(endedAt - windowSeconds * BUCKETS_DRAWN));
      params.set("ended_at", String(endedAt));
      if (scope.app) params.set("app", scope.app);
      if (scope.function) params.set("function", scope.function);
      return apiRequest(
        withWorkspace(`/api/v1/tasks/aggregate-by-time-window?${params.toString()}`, workspace),
        taskTimeWindowBucketListSchema,
      );
    },
  });
}

export function taskMetricsQueryOptions(workspace: string, hours = 24) {
  return queryOptions({
    queryKey: workspaceQueryKeys.tasks.metrics(workspace, hours, null),
    queryFn: () =>
      apiRequest(withWorkspace("/api/v1/tasks/metrics", workspace), taskMetricsSummarySchema),
  });
}

/** Task duration percentiles (p50/p95) plus cold starts for one workload, bucketed over time. */
export function taskLatencyQueryOptions(
  workspace: string,
  app: string,
  name: string,
  windowSeconds = 3600,
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.tasks.latency(workspace, `${app}/${name}`, null, windowSeconds),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/metrics/task-latency?window_seconds=${windowSeconds}`, workspace),
        taskLatencyTimeseriesSchema,
      ),
  });
}
