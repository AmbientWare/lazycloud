import { queryOptions } from "@tanstack/react-query";

import { api, apiRequest, ok, withWorkspace } from "@/lib/api/client";
import { taskLatencyTimeseriesSchema, type Stub } from "@/lib/api/schemas";
import { viewStub } from "@/lib/api/views";
import { workspaceName } from "@/lib/api/workspaces";

import { appById, appDirectory, workloadDirectory } from "./directory";
import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

/**
 * The deployed workloads, each as its active release's stub. Within one app
 * each carries its handler, read from the function's definition.
 */
export function deployedStubsQueryOptions(workspaceId: string, appId?: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.workloads.list(workspaceId, appId ?? null),
    queryFn: async ({ client }): Promise<{ stubs: Stub[] }> => {
      const workspace = workspaceName(workspaceId);
      if (!appId) {
        const [workloads, apps] = await Promise.all([
          workloadDirectory(client, workspaceId),
          appDirectory(client, workspaceId),
        ]);
        return {
          stubs: [...workloads.byId.values()].map((workload) =>
            viewStub(workload, workspaceId, apps.byName.get(workload.app)?.id ?? ""),
          ),
        };
      }
      const app = await appById(client, workspaceId, appId);
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/deployments", {
          params: { path: { workspace }, query: { app: app.name, limit: 1000 } },
        }),
      );
      const stubs = await Promise.all(
        page.deployments.map(async (workload) => {
          const fn = await ok(
            api.GET("/v1/workspaces/{workspace}/apps/{app}/functions/{function}", {
              params: { path: { workspace, app: app.name, function: workload.name } },
            }),
          );
          return viewStub(workload, workspaceId, app.id, fn.active_release.spec.handler);
        }),
      );
      return { stubs };
    },
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
