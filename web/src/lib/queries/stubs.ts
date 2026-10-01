import { queryOptions } from "@tanstack/react-query";

import { api, ok } from "@/lib/api/client";
import type { Stub, TaskLatencyTimeseries } from "@/lib/api/schemas";
import { parseDeploymentId, parseStubId, viewStatusCounts, viewStub } from "@/lib/api/views";
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

/**
 * Task run time percentiles (p50/p95), outcomes and cold starts of one
 * workload, bucketed over the last day. Every stub of a group names the same
 * workload; a deployment ID names it directly.
 */
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
    queryFn: async ({ client }): Promise<TaskLatencyTimeseries> => {
      let workload = options.deploymentId ? parseDeploymentId(options.deploymentId).workload : "";
      if (!workload) {
        const stub = parseStubId(stubIds[0] ?? "");
        const workloads = await workloadDirectory(client, workspaceId);
        workload = workloads.byName.get(`${stub.app}/${stub.name}`)?.id ?? "";
      }
      if (!workload) {
        return {
          workspace_id: workspaceId,
          stub_ids: stubIds,
          deployment_id: "",
          window_seconds: windowSeconds,
          buckets: [],
        };
      }
      const performance = await ok(
        api.GET("/v1/workspaces/{workspace}/deployments/{deployment}/performance", {
          params: {
            path: { workspace: workspaceName(workspaceId), deployment: workload },
            query: { window_seconds: windowSeconds },
          },
        }),
      );
      return {
        workspace_id: workspaceId,
        stub_ids: stubIds,
        deployment_id: options.deploymentId ?? "",
        window_seconds: performance.window_seconds,
        buckets: performance.buckets.map((bucket) => ({
          timestamp: bucket.timestamp,
          count: bucket.count,
          p50_ms: bucket.p50_ms ?? null,
          p95_ms: bucket.p95_ms ?? null,
          cold_starts: bucket.cold_starts,
          status_counts: viewStatusCounts(bucket.status_counts),
        })),
      };
    },
    enabled: stubIds.length > 0,
    meta: workspaceLiveQueryMeta(true),
  });
}
