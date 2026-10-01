import { infiniteQueryOptions, mutationOptions, queryOptions } from "@tanstack/react-query";

import { api, ok } from "@/lib/api/client";
import type {
  Container,
  ContainerDetail,
  ContainerMetricsTimeseries,
  ContainerWithAppPage,
} from "@/lib/api/schemas";
import {
  parseStubId,
  viewActiveDeployment,
  viewApp,
  viewContainer,
  viewStub,
} from "@/lib/api/views";
import { workspaceName } from "@/lib/api/workspaces";

import { appById, appDirectory, workloadDirectory } from "./directory";

import {
  LIVE_LIST_MAX_PAGES,
  nextListCursor,
  selectInfiniteList,
  type InfiniteListQueryData,
} from "./infinite-list";
import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

export type ContainerListOptions = {
  appId?: string;
  stubIds?: string[];
  statuses?: ContainerListStatus[];
  enabled?: boolean;
};

export type ContainerListStatus = "pending" | "running" | "exited" | "failed" | "stopped";

export function containersQueryOptions(workspaceId: string, options: ContainerListOptions = {}) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.containers.list(workspaceId, {
      appId: options.appId ?? null,
      stubIds: options.stubIds?.join(",") ?? null,
      statuses: options.statuses?.join(",") ?? null,
    }),
    initialPageParam: "",
    // The API lists the workspace's containers, or its live ones; the app,
    // workload and status narrowing happens here on that page.
    queryFn: async ({ pageParam, client }): Promise<ContainerWithAppPage> => {
      const live =
        options.statuses !== undefined &&
        options.statuses.every((status) => status === "pending" || status === "running");
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/containers", {
          params: {
            path: { workspace: workspaceName(workspaceId) },
            query: { live, limit: live ? 1000 : 100, cursor: pageParam || undefined },
          },
        }),
      );
      const apps = await appDirectory(client, workspaceId);
      const app = options.appId ? await appById(client, workspaceId, options.appId) : undefined;
      const workloads = new Set(
        (options.stubIds ?? []).map((id) => {
          const stub = parseStubId(id);
          return `${stub.app}/${stub.name}`;
        }),
      );
      const data = page.containers
        .filter((container) => !app || container.app === app.name)
        .filter(
          (container) => !workloads.size || workloads.has(`${container.app}/${container.function}`),
        )
        .map((container) => {
          const appId = apps.byName.get(container.app)?.id ?? "";
          return { container: viewContainer(container, workspaceId, appId), app_id: appId };
        })
        .filter(
          ({ container }) =>
            !options.statuses || options.statuses.includes(container.status as ContainerListStatus),
        );
      return { data, next: page.next_cursor ?? "" };
    },
    getNextPageParam: nextListCursor,
    maxPages: LIVE_LIST_MAX_PAGES,
    enabled: options.enabled,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function selectContainerList(
  data: InfiniteListQueryData<ContainerWithAppPage["data"][number]> | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectInfiniteList(data, hasNextPage, (item) => item.container.id);
}

export function containerQueryOptions(workspaceId: string, containerId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.containers.detail(workspaceId, containerId),
    queryFn: async ({ client }): Promise<ContainerDetail> => {
      const container = await ok(
        api.GET("/v1/workspaces/{workspace}/containers/{container}", {
          params: { path: { workspace: workspaceName(workspaceId), container: containerId } },
        }),
      );
      const [apps, workloads] = await Promise.all([
        appDirectory(client, workspaceId),
        workloadDirectory(client, workspaceId),
      ]);
      const app = apps.byName.get(container.app);
      const workload = workloads.byName.get(`${container.app}/${container.function}`);
      const view = viewContainer(container, workspaceId, app?.id ?? null);
      return {
        ...view,
        app: app ? viewApp(app, workspaceId) : null,
        workload: workload ? viewStub(workload, workspaceId, app?.id ?? "") : null,
        deployment: workload ? viewActiveDeployment(workload, app?.id ?? "") : null,
        run_name: null,
        run_status: null,
        expires_at: null,
        actions: {
          can_stop: container.state !== "stopped",
          can_shell: false,
          can_create_image: false,
          can_snapshot_memory: false,
        },
      };
    },
    meta: workspaceLiveQueryMeta(true),
  });
}

export function stopContainerMutationOptions(workspaceId: string, containerId: string) {
  return mutationOptions({
    mutationFn: async (): Promise<Container> =>
      viewContainer(
        await ok(
          api.POST("/v1/workspaces/{workspace}/containers/{container}/stop", {
            params: { path: { workspace: workspaceName(workspaceId), container: containerId } },
          }),
        ),
        workspaceId,
        null,
      ),
  });
}

export function containerMetricsTimeseriesQueryOptions(
  workspaceId: string,
  containerId: string,
  live: boolean,
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.containers.metrics(workspaceId, containerId),
    // The container's last hour of life, at the finest step the API keeps.
    queryFn: async (): Promise<ContainerMetricsTimeseries> => {
      const metrics = await ok(
        api.GET("/v1/workspaces/{workspace}/containers/{container}/metrics", {
          params: { path: { workspace: workspaceName(workspaceId), container: containerId } },
        }),
      );
      const cpuTotal = metrics.cpu_total_millicores;
      return {
        container_id: metrics.container_id,
        points: metrics.points.map((point) => ({
          timestamp: point.timestamp,
          sample_interval_ms: point.interval_ms,
          cpu_millicores: point.cpu_millicores,
          cpu_total_millicores: cpuTotal,
          cpu_pct: cpuTotal > 0 ? (point.cpu_millicores / cpuTotal) * 100 : 0,
          memory_rss_bytes: point.memory_rss_bytes,
          memory_total_bytes: metrics.memory_total_bytes,
          network_recv_bytes: point.network_recv_bytes,
          network_sent_bytes: point.network_sent_bytes,
          disk_read_bytes: point.disk_read_bytes,
          disk_write_bytes: point.disk_write_bytes,
          // The API samples no root disk usage.
          disk_used_bytes: null,
          disk_total_bytes: null,
          gpu_memory_used_bytes: point.gpu_memory_used_bytes ?? 0,
          gpu_memory_total_bytes: point.gpu_memory_total_bytes ?? 0,
          gpu_type: point.gpu_type ?? "",
        })),
      };
    },
    // Hosts sample every 5 seconds; samples are not change events.
    refetchInterval: live ? 5_000 : false,
  });
}
