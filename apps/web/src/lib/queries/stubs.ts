import { queryOptions } from "@tanstack/react-query";

import { apiRequest, withWorkspace } from "@/lib/api/client";
import { stubListSchema, taskLatencyTimeseriesSchema } from "@/lib/api/schemas";

import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

export function deployedStubsQueryOptions(workspaceId: string, appId?: string) {
  const params = new URLSearchParams({ deployed_only: "true" });
  if (appId) params.set("app_id", appId);
  return queryOptions({
    queryKey: workspaceQueryKeys.workloads.list(workspaceId, appId ?? null),
    queryFn: () =>
      apiRequest(withWorkspace(`/api/v1/stubs?${params.toString()}`, workspaceId), stubListSchema),
    meta: workspaceLiveQueryMeta(true),
  });
}

/** Per-stub task-duration percentiles (p50/p95) plus cold starts, bucketed over time. */
export function taskLatencyQueryOptions(
  workspaceId: string,
  stubIds: string[],
  options: { deploymentId?: string; windowSeconds?: number } = {},
) {
  const windowSeconds = options.windowSeconds ?? 3600;
  return queryOptions({
    queryKey: workspaceQueryKeys.tasks.latency(
      workspaceId,
      [...stubIds].sort().join(","),
      options.deploymentId ?? null,
      windowSeconds,
    ),
    queryFn: () => {
      const params = new URLSearchParams();
      for (const stubId of stubIds) params.append("stub_id", stubId);
      params.set("window_seconds", String(windowSeconds));
      if (options.deploymentId) params.set("deployment_id", options.deploymentId);
      return apiRequest(
        withWorkspace(`/api/v1/metrics/task-latency?${params.toString()}`, workspaceId),
        taskLatencyTimeseriesSchema,
      );
    },
    enabled: stubIds.length > 0,
    meta: workspaceLiveQueryMeta(true),
  });
}
