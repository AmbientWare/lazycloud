import { queryOptions } from "@tanstack/react-query";

import { apiRequest, withWorkspace } from "@/lib/api/client";
import {
  stubListSchema,
  taskLatencyTimeseriesSchema,
  type DeploymentKind,
} from "@/lib/api/schemas";

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

export function taskLatencyQueryOptions(
  workspaceId: string,
  appId: string,
  workloadName: string,
  kind: DeploymentKind,
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.tasks.latency(
      workspaceId,
      `${appId}:${kind}:${workloadName}`,
      null,
      3600,
    ),
    queryFn: () => {
      const params = new URLSearchParams({
        app_id: appId,
        workload_name: workloadName,
        workload_kind: kind,
        window_seconds: "3600",
      });
      return apiRequest(
        withWorkspace(`/api/v1/metrics/task-latency?${params}`, workspaceId),
        taskLatencyTimeseriesSchema,
      );
    },
    meta: workspaceLiveQueryMeta(true),
  });
}
